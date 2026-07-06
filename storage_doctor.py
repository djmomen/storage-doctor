#!/usr/bin/env python3
"""Storage Doctor — deep macOS storage scanner & cleaner.

Scans known space-eaters by category, flags risk, lets you select
items (or whole categories) and clean them — using each tool's native
clean command (brew cleanup, npm cache clean, ...) where one exists,
and a macOS admin prompt when sudo is needed.

Run:  python3 storage_doctor.py
"""

import os
import plistlib
import re
import shlex
import subprocess
import threading
import shutil
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont

HOME = os.path.expanduser("~")

# ---------------------------------------------------------------- helpers

def du_bytes(path: str) -> int:
    """Apparent (logical) size — the number Finder / System Settings show,
    which is what the user compares against. Plain `du` reports physical
    allocated blocks and over-reports ~5-7% on APFS. `-A` gives apparent
    size; fall back to physical on the rare system where `-A` is absent so
    we never silently report 0 for a real directory."""
    for flags in ("-skA", "-sk"):
        try:
            out = subprocess.run(["du", flags, path],
                                 capture_output=True, text=True, timeout=600)
            line = out.stdout.strip()
            if line:
                return int(line.split()[0]) * 1024
        except Exception:
            continue
    return 0


def disk_stats():
    """(total, used, free) matching System Settings / Finder. Reads the APFS
    container from `diskutil` so `free` includes purgeable space and `total`
    is the real container size — `shutil.disk_usage` undercounts both on APFS.
    Falls back to shutil if diskutil is unavailable."""
    try:
        out = subprocess.run(["diskutil", "info", "/"],
                             capture_output=True, text=True, timeout=10).stdout
        total = free = None
        for line in out.splitlines():
            m = re.search(r"\((\d+) Bytes\)", line)
            if not m:
                continue
            if "Container Total Space" in line:
                total = int(m.group(1))
            elif "Container Free Space" in line:
                free = int(m.group(1))
        if total and free is not None:
            return total, total - free, free
    except Exception:
        pass
    u = shutil.disk_usage(HOME)
    return u.total, u.used, u.free


def fmt_size(n: float) -> str:
    n = max(n, 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def age_days(path: str) -> int:
    try:
        return int((time.time() - os.path.getmtime(path)) / 86400)
    except Exception:
        return -1


def which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def notify(msg: str, title: str = "Storage Doctor"):
    """Native macOS notification banner + sound."""
    msg = msg.replace('"', "'")
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{msg}" with title "{title}" sound name "Glass"'],
        capture_output=True, timeout=10)


NEVER_KILL = {"kernel_task", "launchd", "WindowServer", "loginwindow",
              "Finder", "Dock", "SystemUIServer", "launchservicesd",
              "WindowManager", "coreaudiod", "securityd", "opendirectoryd"}


def proc_safety(path: str, name: str) -> str:
    """SAFE = your app, fine to quit. CAUTION = may lose unsaved work.
    SYSTEM = macOS itself, never terminate."""
    if name in NEVER_KILL or path.startswith(
            ("/System/", "/usr/", "/sbin/", "/bin/")):
        return "SYSTEM"
    low = name.lower()
    if any(k in low for k in ("helper", "renderer", "plugin", "gpu",
                              "utility", "extension", "node", "npm",
                              "mdworker", "cache")):
        return "SAFE"
    return "CAUTION"


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def mem_stats():
    """(total, used, compressed, swap_str). `used` = active+wired+compressed
    pages (the 'app memory' + wired Activity Monitor shows), matching what a
    user reads as RAM in use."""
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5).stdout.strip() or 0)
    out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                         timeout=5).stdout
    m = re.search(r"page size of (\d+)", out)
    ps = int(m.group(1)) if m else 4096

    def g(key):
        mm = re.search(re.escape(key) + r":\s+(\d+)", out)
        return int(mm.group(1)) if mm else 0

    comp = g("Pages occupied by compressor") * ps
    used = (g("Pages active") + g("Pages wired down")) * ps + comp
    swap = "0"
    try:
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                            capture_output=True, text=True, timeout=5).stdout
        swap = sw.split("used =")[1].split()[0]
    except Exception:
        pass
    return total, used, comp, swap


# ---------------------------------------------------------------- targets
# SAFE    - regenerated automatically, zero risk
# CAUTION - safe but side effects (re-downloads, slower first run)
# RISKY   - real data dies (VMs, containers) - review first
# INFO    - awareness only, not deletable here

RISK_SAFE, RISK_CAUTION, RISK_RISKY, RISK_INFO = "SAFE", "CAUTION", "RISKY", "INFO"


class Target:
    def __init__(self, category, name, path, risk, note, clean):
        self.category = category
        self.name = name
        self.path = path
        self.risk = risk
        self.note = note
        self.clean = clean  # ("rm",p) | ("rm_contents",p) | ("sudo_rm_contents",p) | ("cmd",[argv]) | ("info",None)
        self.size = 0       # before
        self.after = None   # after clean (None = not cleaned yet)
        self.age = -1

    @property
    def freed(self):
        return self.size - self.after if self.after is not None else 0


def build_targets():
    t = []

    def add(cat, name, path, risk, note, clean=None):
        if path and not os.path.exists(path):
            return
        t.append(Target(cat, name, path, risk, note, clean or ("rm", path)))

    cat = "Package Managers"
    if which("brew"):
        add(cat, "Homebrew cache + old versions",
            os.path.join(HOME, "Library/Caches/Homebrew"), RISK_SAFE,
            "brew cleanup -s --prune=all; re-downloads on next install",
            ("cmd", ["brew", "cleanup", "-s", "--prune=all"]))
    if which("npm"):
        add(cat, "npm cache", os.path.join(HOME, ".npm"), RISK_SAFE,
            "npm cache clean --force; re-downloads packages",
            ("cmd", ["npm", "cache", "clean", "--force"]))
    if which("bun"):
        add(cat, "bun cache", os.path.join(HOME, ".bun/install/cache"),
            RISK_SAFE, "bun pm cache rm", ("cmd", ["bun", "pm", "cache", "rm"]))
    if which("uv"):
        add(cat, "uv cache", os.path.join(HOME, ".cache/uv"), RISK_SAFE,
            "uv cache clean", ("cmd", ["uv", "cache", "clean"]))
    if which("pip3"):
        add(cat, "pip cache", os.path.join(HOME, "Library/Caches/pip"),
            RISK_SAFE, "pip cache purge", ("cmd", ["pip3", "cache", "purge"]))
    if which("yarn"):
        add(cat, "yarn cache", os.path.join(HOME, "Library/Caches/Yarn"),
            RISK_SAFE, "yarn cache clean", ("cmd", ["yarn", "cache", "clean"]))
    if which("pnpm"):
        add(cat, "pnpm store", os.path.join(HOME, "Library/pnpm/store"),
            RISK_SAFE, "pnpm store prune", ("cmd", ["pnpm", "store", "prune"]))
    if which("go"):
        add(cat, "Go build cache", os.path.join(HOME, "Library/Caches/go-build"),
            RISK_SAFE, "go clean -cache", ("cmd", ["go", "clean", "-cache"]))
    if which("cargo"):
        add(cat, "Cargo registry cache",
            os.path.join(HOME, ".cargo/registry/cache"), RISK_SAFE,
            "re-downloads crates on next build")
    add(cat, "CocoaPods cache", os.path.join(HOME, "Library/Caches/CocoaPods"),
        RISK_SAFE, "re-downloads pods")
    add(cat, "Gradle caches", os.path.join(HOME, ".gradle/caches"), RISK_SAFE,
        "re-downloads dependencies; first build slower")
    add(cat, "Maven repository", os.path.join(HOME, ".m2/repository"),
        RISK_SAFE, "re-downloads jars on next build")
    add(cat, "NuGet packages", os.path.join(HOME, ".nuget/packages"),
        RISK_SAFE, "re-downloads .NET packages")
    add(cat, ".NET package cache", os.path.join(HOME, ".dotnet"),
        RISK_CAUTION, "re-downloads .NET SDK bits")
    add(cat, "RubyGems cache", os.path.join(HOME, ".gem"),
        RISK_SAFE, "re-downloads gems")
    if which("rustup"):
        add(cat, "Rust toolchains", os.path.join(HOME, ".rustup/toolchains"),
            RISK_CAUTION, "rustup toolchain install re-fetches; needed to build")
    add(cat, "Nix store note", os.path.join(HOME, ".cache/nix"), RISK_SAFE,
        "nix eval cache; rebuilds")

    cat = "Caches & Temp"
    add(cat, "System temp (/private/tmp)", "/private/tmp", RISK_CAUTION,
        "needs admin; running apps may hold temp files",
        ("sudo_rm_contents", "/private/tmp"))
    add(cat, "User logs (~/Library/Logs)", os.path.join(HOME, "Library/Logs"),
        RISK_SAFE, "diagnostic logs only",
        ("rm_contents", os.path.join(HOME, "Library/Logs")))
    add(cat, "Saved app state",
        os.path.join(HOME, "Library/Saved Application State"), RISK_SAFE,
        "apps forget window positions",
        ("rm_contents", os.path.join(HOME, "Library/Saved Application State")))
    add(cat, "Trash", os.path.join(HOME, ".Trash"), RISK_CAUTION,
        "permanently empties Trash",
        ("rm_contents", os.path.join(HOME, ".Trash")))

    cat = "Developer Tools"
    add(cat, "Xcode DerivedData",
        os.path.join(HOME, "Library/Developer/Xcode/DerivedData"), RISK_SAFE,
        "rebuilds on next compile")
    add(cat, "Xcode Archives",
        os.path.join(HOME, "Library/Developer/Xcode/Archives"), RISK_CAUTION,
        "old app archives; keep if you re-symbolicate crash logs")
    add(cat, "iOS DeviceSupport",
        os.path.join(HOME, "Library/Developer/Xcode/iOS DeviceSupport"),
        RISK_SAFE, "re-copies from device on next connect")
    add(cat, "CoreSimulator devices",
        os.path.join(HOME, "Library/Developer/CoreSimulator/Devices"),
        RISK_CAUTION, "deletes all iOS simulators (recreatable)")
    add(cat, "Cursor cached data",
        os.path.join(HOME, "Library/Application Support/Cursor/CachedData"),
        RISK_SAFE, "editor cache, rebuilds")
    add(cat, "VS Code cached data",
        os.path.join(HOME, "Library/Application Support/Code/CachedData"),
        RISK_SAFE, "editor cache, rebuilds")

    cat = "Containers & VMs"
    add(cat, "OrbStack data (VM + images + volumes)",
        os.path.join(HOME, "Library/Group Containers/HUAQ24HBR6.dev.orbstack"),
        RISK_RISKY,
        "ALL OrbStack containers/images/volumes die. Uninstall app first if unused.")
    add(cat, "Colima VM", os.path.join(HOME, ".colima"), RISK_RISKY,
        "ALL Colima containers die (your Plane runs here!). "
        "Prefer: colima ssh -- docker system prune")
    add(cat, "Docker Desktop data",
        os.path.join(HOME, "Library/Containers/com.docker.docker/Data"),
        RISK_RISKY, "all Docker Desktop images/volumes die")
    if which("docker"):
        t.append(Target(cat, "Docker unused images/cache (prune)", None,
                        RISK_CAUTION,
                        "docker system prune -af; keeps running containers",
                        ("cmd", ["docker", "system", "prune", "-af"])))

    cat = "Browsers"
    add(cat, "Chrome cache", os.path.join(HOME, "Library/Caches/Google/Chrome"),
        RISK_SAFE, "close Chrome first; pages re-cache")
    add(cat, "Chrome Service Worker cache",
        os.path.join(HOME, "Library/Application Support/Google/Chrome/Default/Service Worker/CacheStorage"),
        RISK_CAUTION, "close Chrome first; PWAs re-download")
    add(cat, "Brave cache", os.path.join(HOME, "Library/Caches/BraveSoftware"),
        RISK_SAFE, "close Brave first")
    add(cat, "Firefox cache", os.path.join(HOME, "Library/Caches/Firefox"),
        RISK_SAFE, "close Firefox first")

    return t


def dynamic_targets(progress_cb):
    found = []

    # -- per-app cache breakdown: every cache >50MB becomes its own row --
    progress_cb("Breaking down app caches (deep)...")
    already = {"Homebrew", "pip", "uv", "Yarn", "go-build", "CocoaPods",
               "Google", "BraveSoftware", "Firefox",
               "com.apple.QuickLook.thumbnailcache"}
    for base in (os.path.join(HOME, "Library/Caches"),
                 os.path.join(HOME, ".cache")):
        if not os.path.isdir(base):
            continue
        for d in sorted(os.listdir(base)):
            if d in already or d.startswith("."):
                continue
            p = os.path.join(base, d)
            if not os.path.isdir(p):
                continue
            sz = du_bytes(p)
            if sz > 50 * 1024 * 1024:
                tgt = Target("App Caches (deep)", d, p, RISK_SAFE,
                             "cache — app rebuilds it on next launch",
                             ("rm_contents", p))
                tgt.size = sz
                tgt.age = age_days(p)
                found.append(tgt)

    # -- chat & media app caches --
    progress_cb("Checking chat/media app caches...")
    for name, p, note in (
        ("Slack cache",
         os.path.join(HOME, "Library/Application Support/Slack/Cache"),
         "re-downloads on scroll"),
        ("Slack Service Worker",
         os.path.join(HOME, "Library/Application Support/Slack/Service Worker/CacheStorage"),
         "re-downloads"),
        ("Discord cache",
         os.path.join(HOME, "Library/Application Support/discord/Cache"),
         "re-downloads"),
        ("Telegram media cache",
         os.path.join(HOME, "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/account-1000000000/postbox/media"),
         "media re-downloads from Telegram cloud"),
        ("WhatsApp media",
         os.path.join(HOME, "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/Message/Media"),
         "local media copies; originals stay on phone/cloud"),
        ("Microsoft Teams cache",
         os.path.join(HOME, "Library/Group Containers/UBF8T346G9.com.microsoft.teams/Library/Application Support/Microsoft/MSTeams/EBWebView/Default/Cache"),
         "re-downloads"),
    ):
        if os.path.exists(p):
            found.append(Target("Chat & Media Apps", name, p, RISK_CAUTION,
                                note, ("rm_contents", p)))

    # -- generic Electron/Chromium cache sweep: most desktop apps (Notion,
    # Figma, Obsidian, VS Code, Cursor, Spotify, ...) hide GBs here. Every
    # such cache >50 MB becomes its own row. --
    progress_cb("Sweeping Electron/Chromium app caches (deep)...")
    seen = {t.path for t in found}
    appsup = os.path.join(HOME, "Library/Application Support")
    CACHE_SUBDIRS = ("Cache", "Code Cache", "GPUCache", "CachedData",
                     "DawnCache", "ShaderCache", "Service Worker/CacheStorage")
    if os.path.isdir(appsup):
        for app in sorted(os.listdir(appsup)):
            base = os.path.join(appsup, app)
            if app.startswith(".") or not os.path.isdir(base):
                continue
            for sub in CACHE_SUBDIRS:
                p = os.path.join(base, sub)
                if p in seen or not os.path.isdir(p):
                    continue
                sz = du_bytes(p)
                if sz > 50 * 1024 * 1024:
                    tgt = Target("Electron App Caches (deep)",
                                 f"{app} — {sub}", p, RISK_SAFE,
                                 "app cache — rebuilds on next launch",
                                 ("rm_contents", p))
                    tgt.size = sz
                    tgt.age = age_days(p)
                    found.append(tgt)

    # -- python virtualenvs --
    progress_cb("Hunting Python venvs...")
    try:
        out = subprocess.run(
            ["find", HOME, "-maxdepth", "6", "-type", "d",
             "(", "-name", ".venv", "-o", "-name", "venv", ")",
             "-prune", "-not", "-path", f"{HOME}/Library/*"],
            capture_output=True, text=True, timeout=180)
        for p in out.stdout.strip().splitlines():
            if not os.path.exists(os.path.join(p, "pyvenv.cfg")):
                continue
            a = age_days(os.path.dirname(p))
            found.append(Target(
                "Python venvs (per project)", p.replace(HOME, "~"), p,
                RISK_SAFE if a > 30 else RISK_CAUTION,
                "recreate with: uv sync / pip install -r requirements.txt",
                ("rm", p)))
    except Exception:
        pass

    # -- huge individual files anywhere in home --
    progress_cb("Hunting files > 200 MB (this takes a moment)...")
    try:
        out = subprocess.run(
            ["find", HOME, "-xdev", "-type", "f", "-size", "+200M",
             "-not", "-path", "*orbstack*",
             "-not", "-path", f"{HOME}/.colima/*",
             "-not", "-path", f"{HOME}/Library/Containers/com.docker.docker/*"],
            capture_output=True, text=True, timeout=180)
        for p in out.stdout.strip().splitlines():
            tgt = Target("Huge Files (>200 MB)", p.replace(HOME, "~"), p,
                         RISK_CAUTION,
                         "single big file — double-click to reveal in Finder",
                         ("rm", p))
            try:
                tgt.size = os.path.getsize(p)
            except OSError:
                continue
            tgt.age = age_days(p)
            found.append(tgt)
    except Exception:
        pass

    progress_cb("Hunting node_modules across projects...")
    try:
        out = subprocess.run(
            ["find", HOME, "-maxdepth", "6", "-name", "node_modules",
             "-type", "d", "-prune", "-not", "-path", f"{HOME}/Library/*"],
            capture_output=True, text=True, timeout=180)
        for p in out.stdout.strip().splitlines():
            a = age_days(os.path.dirname(p))
            stale = f", project untouched {a}d" if a > 30 else ""
            found.append(Target(
                "node_modules (per project)", p.replace(HOME, "~"), p,
                RISK_SAFE if a > 30 else RISK_CAUTION,
                f"npm install restores it{stale}", ("rm", p)))
    except Exception:
        pass

    progress_cb("Checking old installers in Downloads...")
    dl = os.path.join(HOME, "Downloads")
    if os.path.isdir(dl):
        for f in os.listdir(dl):
            if f.lower().endswith((".dmg", ".pkg", ".iso", ".zip")):
                p = os.path.join(dl, f)
                a = age_days(p)
                if a > 30:
                    found.append(Target(
                        "Stale Downloads (>30 days)", f, p, RISK_CAUTION,
                        f"installer/archive, {a} days old", ("rm", p)))

    # -- installed applications: deep uninstall (bundle + all leftovers) --
    progress_cb("Listing installed applications...")
    seen_apps = set()
    for apps_dir in ("/Applications", os.path.join(HOME, "Applications")):
        if not os.path.isdir(apps_dir):
            continue
        # find .app bundles up to 2 levels deep (catches /Applications/Utilities etc.)
        try:
            out = subprocess.run(
                ["find", apps_dir, "-maxdepth", "2", "-name", "*.app",
                 "-type", "d", "-prune"],
                capture_output=True, text=True, timeout=60)
            for p in sorted(out.stdout.strip().splitlines()):
                f = os.path.basename(p)
                if f.startswith(".") or f in seen_apps:
                    continue
                seen_apps.add(f)
                found.append(Target(
                    "Applications (deep uninstall)", f[:-4], p, RISK_RISKY,
                    "removes app + ALL leftovers (caches, prefs, containers, "
                    "launch agents, receipts) — from / to ~",
                    ("uninstall", p)))
        except Exception:
            pass

    # -- big macOS space-eaters people forget --
    progress_cb("Checking iOS backups, Mail downloads, system caches...")
    ios = os.path.join(HOME, "Library/Application Support/MobileSync/Backup")
    if os.path.isdir(ios):
        found.append(Target(
            "System & Backups", "iOS device backups", ios, RISK_RISKY,
            "old iPhone/iPad backups — re-back-up from device before deleting",
            ("rm_contents", ios)))
    mail = os.path.join(
        HOME, "Library/Containers/com.apple.mail/Data/Library/Mail Downloads")
    if os.path.isdir(mail):
        found.append(Target(
            "System & Backups", "Mail attachment downloads", mail, RISK_SAFE,
            "copies of attachments — originals stay in Mail",
            ("rm_contents", mail)))
    if os.path.isdir("/Library/Caches"):
        found.append(Target(
            "System & Backups", "System caches (/Library/Caches)",
            "/Library/Caches", RISK_CAUTION,
            "needs admin; apps rebuild caches",
            ("sudo_rm_contents", "/Library/Caches")))
    try:
        snaps = subprocess.run(["tmutil", "listlocalsnapshots", "/"],
                               capture_output=True, text=True, timeout=15)
        n_snaps = len([l for l in snaps.stdout.splitlines()
                       if "com.apple" in l])
        if n_snaps:
            found.append(Target(
                "System & Backups",
                f"Time Machine local snapshots ({n_snaps})", None,
                RISK_CAUTION,
                "thins local APFS snapshots (tmutil thinlocalsnapshots)",
                ("cmd", ["tmutil", "thinlocalsnapshots", "/",
                         "999999999999", "4"])))
    except Exception:
        pass

    # -- deep map: biggest folders anywhere in home, not just known caches.
    # This is what makes the scan account for *all* used space, so the total
    # tracks the actual disk instead of only the categories we hard-coded. --
    progress_cb("Mapping largest folders in home (deep)...")
    try:
        out = subprocess.run(["du", "-skA", "-d", "2", HOME],
                             capture_output=True, text=True, timeout=300)
        rows = []
        for ln in out.stdout.splitlines():
            parts = ln.split("\t") if "\t" in ln else ln.rsplit(None, 1)
            if len(parts) != 2:
                continue
            kb, path = parts
            if not kb.isdigit() or path == HOME:
                continue
            sz = int(kb) * 1024
            if sz > 1024 ** 3:          # only folders bigger than 1 GB
                rows.append((sz, path))
        rows.sort(reverse=True)
        for sz, path in rows[:25]:
            tgt = Target("Largest Folders (info only)", path.replace(HOME, "~"),
                         path, RISK_INFO,
                         "biggest folders on disk — double-click to reveal, "
                         "review manually", ("info", None))
            tgt.size = sz
            tgt.age = age_days(path)
            found.append(tgt)
    except Exception:
        pass
    return found


# ---------------------------------------------------------------- cleaning

def sudo_shell(cmd: str, timeout=600):
    """Run a shell command as root via the native macOS admin dialog —
    the single, standard place the user enters their password."""
    notify("Action needed: enter your admin password in the dialog")
    esc = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = f'do shell script "{esc}" with administrator privileges'
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stderr.strip()[:200] or "ok (admin)")


# -- deep app uninstall: bundle + every leftover from / to ~ ---------------

LEFTOVER_DIRS = [
    "Library/Application Support", "Library/Caches", "Library/Preferences",
    "Library/Logs", "Library/Saved Application State", "Library/WebKit",
    "Library/HTTPStorages", "Library/Containers", "Library/Group Containers",
    "Library/LaunchAgents", "Library/Cookies",
    "Library/Application Scripts",
]
SYSTEM_LEFTOVER_DIRS = [
    "/Library/Application Support", "/Library/Caches",
    "/Library/Preferences", "/Library/LaunchAgents",
    "/Library/LaunchDaemons", "/Library/Logs", "/private/var/db/receipts",
]


def bundle_id(app_path: str):
    try:
        with open(os.path.join(app_path, "Contents/Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier")
    except Exception:
        return None


def find_leftovers(app_path: str):
    """Everything an app left behind: matched by bundle id and app name."""
    bid = bundle_id(app_path)
    name = os.path.basename(app_path)[:-4]  # strip .app
    hits = []
    roots = [os.path.join(HOME, d) for d in LEFTOVER_DIRS] + SYSTEM_LEFTOVER_DIRS
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                # exact name dir, or anything prefixed by the bundle id
                if entry == name or (bid and entry.startswith(bid)):
                    hits.append(os.path.join(root, entry))
        except PermissionError:
            continue
    return bid, hits


def uninstall_app(app_path: str, log):
    """Remove app bundle + all leftovers. Root-owned paths go through sudo."""
    if not os.path.isdir(app_path):
        return False, "app not found"
    bid, leftovers = find_leftovers(app_path)
    log(f"   bundle id: {bid or 'unknown'}")
    # kill the app first so files aren't held open
    subprocess.run(["pkill", "-f", app_path], capture_output=True, timeout=10)
    user_paths, root_paths = [], [app_path]
    for p in leftovers:
        (user_paths if p.startswith(HOME) else root_paths).append(p)
    for p in user_paths:
        log(f"   rm {p.replace(HOME, '~')}")
        subprocess.run(["rm", "-rf", p], capture_output=True, timeout=300)
    for p in root_paths:
        log(f"   rm (admin) {p}")
    ok, msg = sudo_shell(
        "rm -rf " + " ".join(shlex.quote(p) for p in root_paths))
    if not ok:
        return False, f"admin removal failed: {msg}"
    if bid:  # forget installer receipts too
        sudo_shell(f"pkgutil --pkgs | grep -F {shlex.quote(bid)} | "
                   f"xargs -n1 pkgutil --forget 2>/dev/null || true",
                   timeout=60)
    log(f"   removed {1 + len(leftovers)} paths")
    return True, f"uninstalled ({len(leftovers)} leftovers swept)"


def clean_target(tgt: Target, log=lambda m: None):
    kind, arg = tgt.clean
    try:
        if kind == "info":
            return False, "info-only item"
        if kind == "cmd":
            r = subprocess.run(arg, capture_output=True, text=True, timeout=600)
            return r.returncode == 0, (r.stderr.strip()[:200] or "ok")
        if kind == "rm":
            subprocess.run(["rm", "-rf", arg], check=True, timeout=600)
            return True, "deleted"
        if kind == "rm_contents":
            for item in os.listdir(arg):
                subprocess.run(["rm", "-rf", os.path.join(arg, item)],
                               timeout=600)
            return True, "emptied"
        if kind == "sudo_rm_contents":
            return sudo_shell(f"find {shlex.quote(arg)} -mindepth 1 "
                              f"-maxdepth 1 -exec rm -rf {{}} +",
                              timeout=300)
        if kind == "uninstall":
            return uninstall_app(arg, log)
    except Exception as e:
        return False, str(e)[:200]
    return False, "unknown clean method"


DEV_CLEANER_URL = ("https://raw.githubusercontent.com/jemishavasoya/"
                   "dev-cleaner/main/dev-cleaner.sh")


# ---------------------------------------------------------------- GUI

BG = "#111827"          # dashboard background
FG = "#f9fafb"
ACCENT = "#38bdf8"
RISK_COLORS = {RISK_SAFE: "#15803d", RISK_CAUTION: "#b45309",
               RISK_RISKY: "#b91c1c", RISK_INFO: "#6b7280"}
RISK_ICONS = {RISK_SAFE: "🟢", RISK_CAUTION: "🟠",
              RISK_RISKY: "🔴", RISK_INFO: "⚪"}


class CleanWindow:
    """Progress bar + live log shown while cleaning runs."""

    def __init__(self, root, total):
        self.win = tk.Toplevel(root)
        self.win.title("Cleaning…")
        self.win.geometry("680x440")
        self.bar = ttk.Progressbar(self.win, maximum=max(total, 1), value=0)
        self.bar.pack(fill="x", padx=12, pady=(12, 4))
        self.label = ttk.Label(self.win, text="Starting…")
        self.label.pack(fill="x", padx=12)
        self.text = tk.Text(self.win, bg=BG, fg="#e5e7eb", wrap="word",
                            state="disabled", font=("Menlo", 11))
        self.text.pack(fill="both", expand=True, padx=12, pady=(6, 6))
        self.btn = ttk.Button(self.win, text="Close",
                              command=self.win.destroy, state="disabled")
        self.btn.pack(pady=(0, 10))
        # keep window alive until work finishes
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)

    def log(self, msg):
        try:
            self.text.config(state="normal")
            self.text.insert("end", msg + "\n")
            self.text.see("end")
            self.text.config(state="disabled")
        except tk.TclError:
            pass

    def step(self, value, msg):
        try:
            self.bar.config(value=value)
            self.label.config(text=msg)
        except tk.TclError:
            pass

    def done(self):
        try:
            self.win.title("Cleaning — done")
            self.bar.config(value=self.bar["maximum"])
            self.btn.config(state="normal")
            self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)
        except tk.TclError:
            pass


class ProcTable:
    """Reusable process table with 🟢 SAFE / 🟠 CAUTION / ⚪ SYSTEM category
    rows and checkbox selection — click a row to tick it, click a category
    header to tick the whole group (same behaviour as the Storage tab's
    categories). Used by the RAM tab; each metric column is caller-defined."""

    ICONS = {"SAFE": "🟢", "CAUTION": "🟠", "SYSTEM": "⚪"}

    def __init__(self, parent, columns,
                 heading0="Process — click ☐ to select, category to select all"):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in columns],
                                 show="tree headings", selectmode="none")
        self.tree.heading("#0", text=heading0)
        self.tree.column("#0", width=430, anchor="w")
        for cid, label, w in columns:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=w, anchor="center")
        self.tree.tag_configure("hot", foreground="#ef4444")
        self.tree.tag_configure("warm", foreground="#f59e0b")
        self.tree.tag_configure("SYSTEM", foreground="#6b7280")
        self.tree.tag_configure("hcat", font=("", 12, "bold"))
        for safety in ("SAFE", "CAUTION", "SYSTEM"):
            self.tree.insert("", "end", iid=f"cat::{safety}", open=True,
                             tags=("hcat",),
                             text=self.ICONS[safety] + f" {safety}")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self._click)
        self.checked = set()   # pids (str)
        self.info = {}         # pid -> (name, safety)

    def _cat(self, safety):
        cid = f"cat::{safety}"
        if not self.tree.exists(cid):
            self.tree.insert("", "end", iid=cid, open=True, tags=("hcat",),
                             text=self.ICONS[safety] + f" {safety}")
        return cid

    def _row_text(self, iid):
        name, safety = self.info.get(iid, ("?", ""))
        mark = ("☑" if iid in self.checked
                else "—" if safety == "SYSTEM" else "☐")
        self.tree.item(iid, text=f"{mark}  {name}")

    def _update_cat(self, safety):
        cid = f"cat::{safety}"
        if not self.tree.exists(cid):
            return
        kids = self.tree.get_children(cid)
        mark = "" if safety == "SYSTEM" else (
            "☑ " if kids and all(k in self.checked for k in kids) else "☐ ")
        self.tree.item(cid, text=f"{mark}{self.ICONS[safety]} "
                                 f"{safety} — {len(kids)} processes")

    def _click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid.startswith("cat::"):
            safety = iid.split("::", 1)[1]
            if safety == "SYSTEM":
                return
            kids = self.tree.get_children(iid)
            all_on = kids and all(k in self.checked for k in kids)
            for k in kids:
                self.checked.discard(k) if all_on else self.checked.add(k)
                self._row_text(k)
            self._update_cat(safety)
            return
        _, safety = self.info.get(iid, ("", "SYSTEM"))
        if safety == "SYSTEM":
            return
        self.checked.discard(iid) if iid in self.checked else \
            self.checked.add(iid)
        self._row_text(iid)
        self._update_cat(safety)

    def select_safe(self):
        for pid, (_, safety) in self.info.items():
            if safety == "SAFE" and self.tree.exists(pid):
                self.checked.add(pid)
                self._row_text(pid)
        for s in ("SAFE", "CAUTION"):
            self._update_cat(s)

    def deselect(self):
        for pid in list(self.checked):
            self.checked.discard(pid)
            if self.tree.exists(pid):
                self._row_text(pid)
        for s in ("SAFE", "CAUTION"):
            self._update_cat(s)

    def update(self, rows, sort_key):
        """rows: list of {pid,name,safety,values,tags}. sort_key(tree,iid)->
        float sorts each category descending. Updates in place so checkbox
        state and selection survive a refresh."""
        live = set()
        for r in rows:
            pid = r["pid"]
            live.add(pid)
            self.info[pid] = (r["name"], r["safety"])
            parent = self._cat(r["safety"])
            if self.tree.exists(pid):
                if self.tree.parent(pid) != parent:
                    self.tree.move(pid, parent, "end")
                self.tree.item(pid, values=r["values"], tags=r["tags"])
            else:
                self.tree.insert(parent, "end", iid=pid,
                                 values=r["values"], tags=r["tags"])
            self._row_text(pid)
        for parent in self.tree.get_children(""):
            for iid in self.tree.get_children(parent):
                if iid not in live:
                    self.tree.delete(iid)
                    self.checked.discard(iid)
                    self.info.pop(iid, None)
        for parent in self.tree.get_children(""):
            kids = sorted(self.tree.get_children(parent),
                          key=lambda i: sort_key(self.tree, i), reverse=True)
            for pos, k in enumerate(kids):
                self.tree.move(k, parent, pos)
            self._update_cat(parent.split("::", 1)[1])


class App:
    def __init__(self, root):
        self.root = root
        root.title("Storage Doctor")
        root.geometry("1180x780")
        root.minsize(980, 640)
        self.targets = {}       # tree iid -> Target
        self.checked = set()
        self.session_freed = 0
        self._batch_start = 0
        self.clean_win = None
        self.q = queue.Queue()
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.apply_filter())

        self._build_dashboard()

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.tab_storage = ttk.Frame(self.nb)
        self.tab_heat = ttk.Frame(self.nb)
        self.tab_ram = ttk.Frame(self.nb)
        self.nb.add(self.tab_storage, text="💾 Storage")
        self.nb.add(self.tab_heat, text="🌡 Heat / CPU")
        self.nb.add(self.tab_ram, text="🧠 RAM")

        self._build_toolbar(self.tab_storage)
        self._build_tree(self.tab_storage)
        self._build_heat_tab(self.tab_heat)
        self._build_ram_tab(self.tab_ram)

        self.status = ttk.Label(root, text="Ready — click  🔍 Deep Scan",
                                padding=(10, 6))
        self.status.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(root, mode="indeterminate")

        self.refresh_dashboard()
        self.root.after(120, self.poll)

        # force window to front when launched from Finder/Dock, then
        # kick off the scan automatically — no extra click needed
        root.lift()
        root.attributes("-topmost", True)
        root.after(800, lambda: root.attributes("-topmost", False))
        root.focus_force()
        root.after(300, self.scan)

    # ---------- dashboard ----------
    def _build_dashboard(self):
        dash = tk.Frame(self.root, bg=BG, padx=16, pady=12)
        dash.pack(fill="x")
        big = tkfont.Font(size=20, weight="bold")
        small = tkfont.Font(size=11)

        # disk gauge (left)
        left = tk.Frame(dash, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        self.disk_label = tk.Label(left, text="", bg=BG, fg=FG, font=big,
                                   anchor="w")
        self.disk_label.pack(fill="x")
        bar_row = tk.Frame(left, bg=BG)
        bar_row.pack(fill="x", pady=(6, 2))
        self.disk_canvas = tk.Canvas(bar_row, height=14, bg="#374151",
                                     highlightthickness=0)
        self.disk_canvas.pack(fill="x")
        self.disk_sub = tk.Label(left, text="", bg=BG, fg="#9ca3af",
                                 font=small, anchor="w")
        self.disk_sub.pack(fill="x")

        # metric tiles (right)
        tiles = tk.Frame(dash, bg=BG)
        tiles.pack(side="right", padx=(24, 0))
        self.tile_vars = {}
        for key, label, color in (
                ("reclaim", "Reclaimable", ACCENT),
                ("selected", "Selected", "#facc15"),
                ("freed", "Freed this session", "#4ade80"),
                ("items", "Items", "#e5e7eb")):
            f = tk.Frame(tiles, bg="#1f2937", padx=14, pady=8)
            f.pack(side="left", padx=5)
            v = tk.Label(f, text="—", bg="#1f2937", fg=color, font=big)
            v.pack()
            tk.Label(f, text=label, bg="#1f2937", fg="#9ca3af",
                     font=small).pack()
            self.tile_vars[key] = v

    def refresh_dashboard(self):
        try:
            total, used, free = disk_stats()
            pct = used / total
            self.disk_label.config(
                text=f"Disk  {fmt_size(used)} used of {fmt_size(total)}")
            self.disk_sub.config(
                text=f"{fmt_size(free)} free  ·  {pct:.0%} full")
            self.disk_canvas.delete("all")
            w = self.disk_canvas.winfo_width() or 500
            color = "#ef4444" if pct > 0.9 else "#f59e0b" if pct > 0.75 else "#22c55e"
            self.disk_canvas.create_rectangle(0, 0, w * pct, 14,
                                              fill=color, width=0)
        except Exception:
            pass
        reclaim = sum(t.size for t in self.targets.values()
                      if t.risk != RISK_INFO and t.after is None)
        sel = sum(self.targets[i].size for i in self.checked)
        self.tile_vars["reclaim"].config(text=fmt_size(reclaim) if self.targets else "—")
        self.tile_vars["selected"].config(text=fmt_size(sel) if self.checked else "0")
        self.tile_vars["freed"].config(text=fmt_size(self.session_freed))
        self.tile_vars["items"].config(text=str(len(self.targets)) if self.targets else "—")

    # ---------- toolbar ----------
    def _build_toolbar(self, parent):
        top = ttk.Frame(parent, padding=(10, 8))
        top.pack(fill="x")
        self.btn_scan = ttk.Button(top, text="🔍 Deep Scan", command=self.scan)
        self.btn_scan.pack(side="left")
        ttk.Button(top, text="Select all SAFE",
                   command=lambda: self.select_risk({RISK_SAFE})
                   ).pack(side="left", padx=(10, 4))
        ttk.Button(top, text="Select all",
                   command=lambda: self.select_risk(
                       {RISK_SAFE, RISK_CAUTION, RISK_RISKY})).pack(side="left")
        ttk.Button(top, text="Deselect",
                   command=self.deselect_all).pack(side="left", padx=4)
        self.btn_dev = ttk.Button(top, text="🧹 dev-cleaner",
                                  command=self.run_dev_cleaner)
        self.btn_dev.pack(side="left", padx=(10, 0))
        self.btn_clean = ttk.Button(top, text="🗑  Clean selected…",
                                    command=self.confirm_clean, state="disabled")
        self.btn_clean.pack(side="right")
        ttk.Entry(top, textvariable=self.search_var, width=22
                  ).pack(side="right", padx=8)
        ttk.Label(top, text="Filter:").pack(side="right")

        # clickable risk chips — click one to select everything at that risk
        chips = tk.Frame(parent, padx=10)
        chips.pack(fill="x")
        self.chip_labels = {}
        for risk in (RISK_SAFE, RISK_CAUTION, RISK_RISKY):
            lbl = tk.Label(chips, text=f"{RISK_ICONS[risk]} {risk}: —",
                           fg="white", bg=RISK_COLORS[risk],
                           padx=10, pady=3, cursor="hand2")
            lbl.pack(side="left", padx=(0, 6), pady=(0, 6))
            lbl.bind("<Button-1>",
                     lambda e, r=risk: self.select_risk({r}))
            self.chip_labels[risk] = lbl
        tk.Label(chips, text="← click a chip to select all items at that "
                 "risk level", fg="#6b7280").pack(side="left")

    def refresh_chips(self):
        for risk, lbl in self.chip_labels.items():
            items = [t for t in self.targets.values()
                     if t.risk == risk and t.after is None]
            lbl.config(text=f"{RISK_ICONS[risk]} {risk}: {len(items)} items · "
                            f"{fmt_size(sum(t.size for t in items))}")

    # ---------- tree ----------
    def _build_tree(self, parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        cols = ("before", "after", "freed", "risk", "age", "note")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings",
                                 selectmode="none")
        heads = {"#0": ("Item — click ☐ to select, double-click to reveal in Finder", 360),
                 "before": ("Size (before)", 100), "after": ("After", 90),
                 "freed": ("Freed", 90), "risk": ("Risk", 90),
                 "age": ("Age", 55), "note": ("What happens if deleted", 330)}
        for c, (txt, w) in heads.items():
            self.tree.heading(c, text=txt)
            anchor = "w" if c in ("#0", "note") else "e" if c in (
                "before", "after", "freed", "age") else "center"
            self.tree.column(c, width=w, anchor=anchor)
        for risk, color in RISK_COLORS.items():
            self.tree.tag_configure(risk, foreground=color)
        self.tree.tag_configure("category", font=("", 13, "bold"))
        self.tree.tag_configure("cleaned", foreground="#16a34a")
        ttk.Style().configure("Treeview", rowheight=26)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", self.on_double)

    # ---------- heat / CPU tab ----------
    def _build_heat_tab(self, parent):
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill="both", expand=True)

        # -- headline verdicts --
        self.load_label = ttk.Label(frame, font=("", 13, "bold"))
        self.load_label.pack(anchor="w")
        self.therm_label = ttk.Label(frame, font=("", 12))
        self.therm_label.pack(anchor="w")
        self.mem_label = ttk.Label(frame, font=("", 12))
        self.mem_label.pack(anchor="w", pady=(0, 6))

        # -- live CPU graph (last 2 minutes) --
        gwrap = tk.Frame(frame, bg=BG)
        gwrap.pack(fill="x", pady=(0, 8))
        tk.Label(gwrap, text="CPU usage — last 2 minutes", bg=BG,
                 fg="#9ca3af", font=("", 10)).pack(anchor="w", padx=8,
                                                   pady=(6, 0))
        self.heat_canvas = tk.Canvas(gwrap, height=90, bg=BG,
                                     highlightthickness=0)
        self.heat_canvas.pack(fill="x", padx=8, pady=(2, 8))
        self.cpu_history = []  # rolling list of overall-CPU %

        # -- action bar --
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Select all SAFE",
                   command=lambda: self.heat_table.select_safe()
                   ).pack(side="left")
        ttk.Button(bar, text="Deselect",
                   command=lambda: self.heat_table.deselect()
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="⛔ Terminate selected…",
                   command=self.kill_selected).pack(side="left", padx=(8, 0))
        ttk.Label(bar, text="  🟢 SAFE quit anytime · 🟠 CAUTION may lose "
                            "unsaved work · ⚪ SYSTEM cannot be selected"
                  ).pack(side="left")

        # -- process table (shared component: categories + checkboxes) --
        self.heat_table = ProcTable(
            frame, [("cpu", "CPU %", 70), ("mem", "MEM %", 70),
                    ("time", "CPU time", 90), ("safety", "Safety", 110)])
        self._refresh_heat()
        self._refresh_therm()

    def _refresh_heat(self):
        ncpu = os.cpu_count() or 1
        try:
            load1, load5, load15 = os.getloadavg()
            flag = "  🔥 HIGH LOAD — machine will heat up" \
                if load1 > ncpu else "  ✅ normal"
            self.load_label.config(
                text=f"Load avg (1/5/15m): {load1:.2f} / {load5:.2f} / "
                     f"{load15:.2f}   ·   {ncpu} cores{flag}")
        except Exception:
            pass
        try:
            _, _, _, swap = mem_stats()
            warn = ("  ⚠️ swapping = extra heat"
                    if swap.rstrip("M.0") not in ("", "0") else "")
            self.mem_label.config(text=f"💾 Swap used: {swap}{warn}")
        except Exception:
            pass
        try:
            out = subprocess.run(
                ["ps", "-Ao", "pid,pcpu,pmem,time,comm", "-r"],
                capture_output=True, text=True, timeout=5)
            lines = out.stdout.strip().splitlines()[1:]
            total_cpu = min(sum(_f(l.split(None, 2)[1]) for l in lines
                                if len(l.split(None, 2)) > 1) / ncpu, 100)
            self.cpu_history = (self.cpu_history + [total_cpu])[-60:]
            self._draw_graph()

            rows = []
            for ln in lines[:30]:
                parts = ln.split(None, 4)
                if len(parts) < 5:
                    continue
                pid, cpu, mem, cput, path = parts
                name = os.path.basename(path)
                safety = proc_safety(path, name)
                icon = ProcTable.ICONS[safety]
                tag = ("hot" if _f(cpu) > 80 else "warm" if _f(cpu) > 30
                       else "SYSTEM" if safety == "SYSTEM" else "")
                rows.append({
                    "pid": pid, "name": name, "safety": safety,
                    "values": (cpu, mem, cput, f"{icon} {safety}"),
                    "tags": (tag,)})
            self.heat_table.update(
                rows, lambda tree, i: _f(tree.set(i, "cpu")))
        except Exception:
            pass
        # ponytail: polls ps every 2s, cheap enough — no daemon/watcher needed
        self.root.after(2000, self._refresh_heat)

    def _draw_graph(self):
        c = self.heat_canvas
        c.delete("all")
        w = c.winfo_width() or 800
        h = 90
        # guide lines at 25/50/75%
        for pct in (25, 50, 75):
            y = h - h * pct / 100
            c.create_line(0, y, w, y, fill="#1f2937")
            c.create_text(4, y, text=f"{pct}%", anchor="w",
                          fill="#4b5563", font=("", 8))
        pts = self.cpu_history
        if len(pts) < 2:
            return
        step = w / 59
        coords = []
        for i, v in enumerate(pts):
            coords += [i * step + (60 - len(pts)) * step, h - h * v / 100]
        cur = pts[-1]
        color = "#ef4444" if cur > 75 else "#f59e0b" if cur > 40 else "#22c55e"
        c.create_line(*coords, fill=color, width=2, smooth=True)
        c.create_text(w - 6, 10, text=f"{cur:.0f}%", anchor="e",
                      fill=color, font=("", 12, "bold"))

    def _refresh_therm(self):
        """Thermal throttling status — the direct 'is my Mac hot' signal."""
        try:
            out = subprocess.run(["pmset", "-g", "therm"],
                                 capture_output=True, text=True, timeout=5)
            limit = 100
            for ln in out.stdout.splitlines():
                if "CPU_Speed_Limit" in ln:
                    limit = int(ln.split("=")[1].strip())
            if limit < 100:
                self.therm_label.config(
                    text=f"🔥 THERMAL THROTTLING: CPU capped at {limit}% — "
                         f"macOS is slowing the CPU to cool down",
                    foreground="#ef4444")
            else:
                self.therm_label.config(
                    text="🌡 Thermal state: OK — no CPU throttling",
                    foreground="#16a34a")
        except Exception:
            self.therm_label.config(text="🌡 Thermal state: unavailable")
        self.root.after(10000, self._refresh_therm)

    def _alive(self, pid):
        # ps -p works regardless of who owns the process
        return subprocess.run(["ps", "-p", pid],
                              capture_output=True).returncode == 0

    def _kill_pids(self, pids):
        """Actually terminate. SIGTERM first (lets apps save), brief grace,
        then SIGKILL survivors, then admin SIGKILL for anything still up.
        Polite SIGTERM alone often does nothing — apps trap it — so a plain
        `kill` looked successful while the process stayed alive. Returns the
        set of pids confirmed dead. (Helper/renderer procs may be respawned
        by their parent app — kill the main app to truly reclaim its RAM.)"""
        pids = [p for p in pids if self._alive(p)]
        for p in pids:
            subprocess.run(["kill", "-15", p], capture_output=True)
        time.sleep(0.8)
        survivors = [p for p in pids if self._alive(p)]
        for p in survivors:
            subprocess.run(["kill", "-9", p], capture_output=True)
        time.sleep(0.4)
        stubborn = [p for p in pids if self._alive(p)]
        if stubborn:   # not ours → one admin prompt for all of them
            sudo_shell("kill -9 " + " ".join(shlex.quote(p) for p in stubborn))
            time.sleep(0.3)
        return {p for p in pids if not self._alive(p)}

    def kill_selected(self):
        picks = [(p, *self.heat_table.info.get(p, ("?", "?")))
                 for p in self.heat_table.checked
                 if self.heat_table.tree.exists(p)]
        if not picks:
            messagebox.showinfo("Heat", "Tick ☐ next to one or more "
                                        "processes first.")
            return
        caution = [n for _, n, s in picks if s == "CAUTION"]
        msg = "Terminate these processes?\n\n" + "\n".join(
            f"  • {n} (PID {p})" for p, n, _ in picks)
        if caution:
            msg += ("\n\n🟠 May lose unsaved work in: "
                    + ", ".join(caution))
        if not messagebox.askyesno("Terminate", msg):
            return
        dead = self._kill_pids([p for p, _, _ in picks])
        self.heat_table.checked.clear()
        for safety in ("SAFE", "CAUTION"):
            self.heat_table._update_cat(safety)
        self.status.config(
            text=f"Terminated {len(dead)}/{len(picks)} process(es)")

    # ---------- RAM tab ----------
    def _build_ram_tab(self, parent):
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill="both", expand=True)
        self.ram_head = ttk.Label(frame, font=("", 13, "bold"))
        self.ram_head.pack(anchor="w")
        self.ram_sub = ttk.Label(frame, font=("", 12))
        self.ram_sub.pack(anchor="w", pady=(0, 6))

        gwrap = tk.Frame(frame, bg=BG)
        gwrap.pack(fill="x", pady=(0, 8))
        tk.Label(gwrap, text="Memory in use", bg=BG, fg="#9ca3af",
                 font=("", 10)).pack(anchor="w", padx=8, pady=(6, 0))
        self.ram_canvas = tk.Canvas(gwrap, height=16, bg="#374151",
                                    highlightthickness=0)
        self.ram_canvas.pack(fill="x", padx=8, pady=(2, 8))

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Select all SAFE",
                   command=lambda: self.ram_table.select_safe()).pack(side="left")
        ttk.Button(bar, text="Deselect",
                   command=lambda: self.ram_table.deselect()).pack(side="left",
                                                                    padx=4)
        ttk.Button(bar, text="⛔ Free RAM (quit selected)…",
                   command=self.ram_free).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="🧹 Purge inactive (admin)",
                   command=self.ram_purge).pack(side="left", padx=(8, 0))
        ttk.Label(bar, text="  🟢 SAFE quit anytime · 🟠 CAUTION may lose "
                            "unsaved work · ⚪ SYSTEM cannot be selected"
                  ).pack(side="left")

        self.ram_table = ProcTable(
            frame, [("mem", "MEM %", 70), ("rss", "RAM", 100),
                    ("safety", "Safety", 110)])
        self._refresh_ram()

    def _refresh_ram(self):
        try:
            total, used, comp, swap = mem_stats()
            pct = used / total if total else 0
            self.ram_head.config(
                text=f"RAM  {fmt_size(used)} used of {fmt_size(total)}  ·  "
                     f"{pct:.0%}")
            warn = "  ⚠️ swapping — free some RAM" if swap.rstrip(
                "M.0") not in ("", "0") else ""
            self.ram_sub.config(
                text=f"Compressed {fmt_size(comp)}   ·   Swap {swap}{warn}")
            c = self.ram_canvas
            c.delete("all")
            w = c.winfo_width() or 500
            color = ("#ef4444" if pct > 0.9 else "#f59e0b" if pct > 0.75
                     else "#22c55e")
            c.create_rectangle(0, 0, w * pct, 16, fill=color, width=0)
        except Exception:
            pass
        try:
            out = subprocess.run(["ps", "-Ao", "pid,pmem,rss,comm", "-m"],
                                 capture_output=True, text=True, timeout=5)
            rows = []
            for ln in out.stdout.strip().splitlines()[1:31]:
                parts = ln.split(None, 3)
                if len(parts) < 4:
                    continue
                pid, mem, rss, path = parts
                name = os.path.basename(path)
                safety = proc_safety(path, name)
                icon = ProcTable.ICONS[safety]
                rss_b = _f(rss) * 1024   # ps rss is KiB
                tag = ("hot" if _f(mem) > 20 else "warm" if _f(mem) > 8
                       else "SYSTEM" if safety == "SYSTEM" else "")
                rows.append({
                    "pid": pid, "name": name, "safety": safety,
                    "values": (mem, fmt_size(rss_b), f"{icon} {safety}"),
                    "tags": (tag,)})
            self.ram_table.update(
                rows, lambda tree, i: _f(tree.set(i, "mem")))
        except Exception:
            pass
        self.root.after(2000, self._refresh_ram)

    def ram_free(self):
        picks = [(p, *self.ram_table.info.get(p, ("?", "?")))
                 for p in self.ram_table.checked if self.ram_table.tree.exists(p)]
        if not picks:
            messagebox.showinfo("RAM", "Tick ☐ next to one or more apps first.")
            return
        caution = [n for _, n, s in picks if s == "CAUTION"]
        msg = "Quit these to free their RAM?\n\n" + "\n".join(
            f"  • {n} (PID {p})" for p, n, _ in picks)
        if caution:
            msg += "\n\n🟠 May lose unsaved work in: " + ", ".join(caution)
        if not messagebox.askyesno("Free RAM", msg):
            return
        dead = self._kill_pids([p for p, _, _ in picks])
        self.ram_table.checked.clear()
        for s in ("SAFE", "CAUTION"):
            self.ram_table._update_cat(s)
        self.status.config(
            text=f"Freed RAM — terminated {len(dead)}/{len(picks)} app(s)")

    def ram_purge(self):
        if not messagebox.askyesno(
                "Purge inactive memory",
                "Flush inactive/cached memory with `sudo purge`?\n\n"
                "Frees cached RAM immediately. Safe — disk caches just "
                "rebuild. Needs admin."):
            return
        ok, msg = sudo_shell("purge")
        self.status.config(text="Purged inactive memory"
                           if ok else f"purge failed: {msg}")

    # ---------- scanning ----------
    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def scan(self):
        self.btn_scan.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.targets.clear()
        self.checked.clear()
        self.refresh_dashboard()
        self.progress.pack(side="bottom", fill="x")
        self.progress.start(10)
        self._spin_i = 0
        self._scanning = True
        self._spin()
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _spin(self):
        if not self._scanning:
            return
        self.btn_scan.config(text=f"{self.SPINNER[self._spin_i]} Scanning…")
        self._spin_i = (self._spin_i + 1) % len(self.SPINNER)
        self.root.after(120, self._spin)

    def _scan_worker(self):
        t0 = time.time()
        say = lambda m: self.q.put(("status", m))
        say("Building target list...")
        targets = build_targets() + dynamic_targets(say)
        self.q.put(("total", len(targets)))
        for i, tgt in enumerate(targets, 1):
            if tgt.path and tgt.size == 0:
                say(f"[{i}/{len(targets)}] Sizing  {tgt.name} …")
                tgt.size = du_bytes(tgt.path)
                tgt.age = age_days(tgt.path)
            self.q.put(("prog", i))
            if tgt.size > 1024 * 100 or tgt.path is None:
                self.q.put(("row", tgt))
        self.q.put(("done", time.time() - t0))

    def poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.config(text=payload)
                elif kind == "total":
                    self.progress.stop()
                    self.progress.config(mode="determinate",
                                         maximum=payload, value=0)
                elif kind == "prog":
                    self.progress.config(value=payload)
                elif kind == "row":
                    self.add_row(payload)
                    self.refresh_dashboard()
                elif kind == "done":
                    self._scanning = False
                    self.progress.stop()
                    self.progress.pack_forget()
                    self.btn_scan.config(state="normal", text="🔍 Deep Scan")
                    self.btn_clean.config(state="normal")
                    self.sort_tree()
                    self.refresh_dashboard()
                    self.refresh_chips()
                    total = sum(t.size for t in self.targets.values()
                                if t.risk != RISK_INFO)
                    self.status.config(
                        text=f"Scan complete in {payload:.0f}s — "
                             f"{fmt_size(total)} reclaimable in "
                             f"{len(self.targets)} items. Tip: click a "
                             f"category's ☐ to select the whole category.")
                    notify(f"Scan complete: {fmt_size(total)} reclaimable "
                           f"in {len(self.targets)} items")
                elif kind == "cleaned":
                    self.mark_cleaned(*payload)
                elif kind == "log":
                    if self.clean_win:
                        self.clean_win.log(payload)
                elif kind == "cprog":
                    if self.clean_win:
                        self.clean_win.step(*payload)
                elif kind == "clean_done":
                    self.btn_clean.config(state="normal")
                    self.btn_dev.config(state="normal")
                    if self.clean_win:
                        self.clean_win.done()
                    self.refresh_dashboard()
                    self.refresh_chips()
                    batch = self.session_freed - self._batch_start
                    notify(f"Cleanup done — freed {fmt_size(batch)} "
                           f"({fmt_size(self.session_freed)} this session)")
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    def cat_id(self, category):
        cid = f"cat::{category}"
        if not self.tree.exists(cid):
            self.tree.insert("", "end", iid=cid, text=f"☐  {category}",
                             open=True, tags=("category",))
        return cid

    def add_row(self, tgt: Target):
        cid = self.cat_id(tgt.category)
        age = f"{tgt.age}d" if tgt.age >= 0 else ""
        iid = self.tree.insert(
            cid, "end", text=f"☐  {tgt.name}",
            values=(fmt_size(tgt.size) if tgt.size else "-", "", "",
                    f"{RISK_ICONS[tgt.risk]} {tgt.risk}", age, tgt.note),
            tags=(tgt.risk,))
        self.targets[iid] = tgt
        self.update_category_row(cid)

    def update_category_row(self, cid):
        kids = self.tree.get_children(cid)
        before = sum(self.targets[k].size for k in kids)
        freed = sum(self.targets[k].freed for k in kids)
        after = before - freed
        self.tree.item(cid, values=(
            fmt_size(before), fmt_size(after) if freed else "",
            f"-{fmt_size(freed)}" if freed else "", "", "",
            f"{len(kids)} items"))
        # category checkbox state
        selectable = [k for k in kids if self.targets[k].risk != RISK_INFO]
        name = self.tree.item(cid, "text")[3:]
        mark = "☑" if selectable and all(
            k in self.checked for k in selectable) else "☐"
        self.tree.item(cid, text=f"{mark}  {name}")

    def sort_tree(self):
        cats = list(self.tree.get_children(""))
        def cat_size(c):
            return sum(self.targets[i].size for i in self.tree.get_children(c))
        for pos, c in enumerate(sorted(cats, key=cat_size, reverse=True)):
            self.tree.move(c, "", pos)
        for c in cats:
            kids = sorted(self.tree.get_children(c),
                          key=lambda i: self.targets[i].size, reverse=True)
            for pos, k in enumerate(kids):
                self.tree.move(k, c, pos)

    def apply_filter(self):
        q = self.search_var.get().lower()
        for cid in self.tree.get_children(""):
            visible = 0
            for k in self.tree.get_children(cid):
                t = self.targets[k]
                show = q in t.name.lower() or q in t.category.lower() \
                    or q in t.risk.lower()
                if show:
                    visible += 1
            self.tree.item(cid, open=bool(q) or True)
            # tkinter cannot hide rows; collapse categories with no match
            self.tree.item(cid, open=(visible > 0 if q else True))

    # ---------- selection ----------
    def on_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid in self.targets:
            self.set_checked(iid, iid not in self.checked)
        elif iid.startswith("cat::"):
            # toggle whole category only when clicking the checkbox area,
            # otherwise let default expand/collapse happen
            if self.tree.identify_element(event.x, event.y) != "Treeitem.indicator":
                kids = [k for k in self.tree.get_children(iid)
                        if self.targets[k].risk != RISK_INFO
                        and self.targets[k].clean[0] != "uninstall"]
                all_on = kids and all(k in self.checked for k in kids)
                for k in kids:
                    self.set_checked(k, not all_on, refresh_cat=False)
                self.update_category_row(iid)
        self.refresh_dashboard()

    def on_double(self, event):
        iid = self.tree.identify_row(event.y)
        t = self.targets.get(iid)
        if t and t.path and os.path.exists(t.path):
            subprocess.run(["open", "-R", t.path])

    def set_checked(self, iid, on, refresh_cat=True):
        tgt = self.targets[iid]
        if tgt.risk == RISK_INFO or tgt.after is not None:
            return
        name = self.tree.item(iid, "text")[3:]
        if on:
            self.checked.add(iid)
            self.tree.item(iid, text=f"☑  {name}")
        else:
            self.checked.discard(iid)
            self.tree.item(iid, text=f"☐  {name}")
        if refresh_cat:
            self.update_category_row(self.tree.parent(iid))

    def select_risk(self, risks):
        for iid, tgt in self.targets.items():
            if tgt.clean[0] == "uninstall":
                continue  # apps are only ever uninstalled one by one, on purpose
            self.set_checked(iid, tgt.risk in risks, refresh_cat=False)
        for cid in self.tree.get_children(""):
            self.update_category_row(cid)
        self.refresh_dashboard()

    def deselect_all(self):
        for iid in list(self.checked):
            self.set_checked(iid, False, refresh_cat=False)
        for cid in self.tree.get_children(""):
            self.update_category_row(cid)
        self.refresh_dashboard()

    # ---------- cleaning ----------
    def confirm_clean(self):
        if not self.checked:
            messagebox.showinfo("Storage Doctor", "Nothing selected.")
            return
        sel = [self.targets[i] for i in self.checked]
        total = sum(t.size for t in sel)
        risky = [t for t in sel if t.risk == RISK_RISKY]
        lines = "\n".join(f"  {RISK_ICONS[t.risk]} {t.name} — {fmt_size(t.size)}"
                          for t in sorted(sel, key=lambda t: -t.size)[:20])
        more = f"\n  … and {len(sel) - 20} more" if len(sel) > 20 else ""
        if not messagebox.askyesno(
                "Confirm cleanup",
                f"Clean {len(sel)} items — reclaim ~{fmt_size(total)}?\n\n"
                f"{lines}{more}"):
            return
        if risky and not messagebox.askyesno(
                "⚠️ RISKY items selected",
                "These destroy real data (VMs, containers, volumes):\n\n"
                + "\n".join(f"  • {t.name}" for t in risky)
                + "\n\nAre you ABSOLUTELY sure?", icon="warning"):
            return
        self.btn_clean.config(state="disabled")
        self.btn_dev.config(state="disabled")
        self._batch_start = self.session_freed
        self.clean_win = CleanWindow(self.root, len(self.checked))
        threading.Thread(target=self._clean_worker,
                         args=(list(self.checked),), daemon=True).start()

    def _clean_worker(self, iids):
        log = lambda m: self.q.put(("log", m))
        needs_sudo = any(self.targets[i].clean[0] in
                         ("sudo_rm_contents", "uninstall") for i in iids)
        if needs_sudo:
            notify("Some items need admin rights — watch for the "
                   "password dialog")
        failed = []
        for i, iid in enumerate(iids, 1):
            tgt = self.targets[iid]
            head = f"[{i}/{len(iids)}] {tgt.name}"
            self.q.put(("cprog", (i - 1, head)))
            self.q.put(("status", f"Cleaning  {tgt.name} …"))
            log(f"→ {tgt.name}  ({fmt_size(tgt.size)})")
            ok, msg = clean_target(tgt, log)
            if ok:
                after = du_bytes(tgt.path) if tgt.path and os.path.exists(
                    tgt.path) else 0
                self.q.put(("cleaned", (iid, after)))
                log(f"   ✅ {msg} — freed {fmt_size(tgt.size - after)}")
            else:
                failed.append(tgt.name)
                log(f"   ❌ FAILED: {msg}")
                self.q.put(("status", f"FAILED  {tgt.name}: {msg}"))
            self.q.put(("cprog", (i, head)))
        if failed:
            notify(f"Action needed — {len(failed)} item(s) failed: "
                   f"{', '.join(failed[:3])}")
            log(f"\n⚠️  Failed: {', '.join(failed)}")
        self.q.put(("clean_done", None))
        self.q.put(("status", "Cleanup finished — sizes updated below "
                              "(before → after → freed)."))

    # ---------- dev-cleaner.sh ----------
    def run_dev_cleaner(self):
        if not messagebox.askyesno(
                "dev-cleaner",
                "Open Terminal and run the community dev-cleaner.sh script?\n"
                "You'll answer its menu prompts yourself."):
            return
        cmd = (f'curl -fsSL {DEV_CLEANER_URL} -o dev-cleanup.sh '
               f'&& chmod +x dev-cleanup.sh && ./dev-cleanup.sh')
        script = ('tell application "Terminal"\n'
                  f'  do script "cd {shlex.quote(HOME)} && {cmd}"\n'
                  '  activate\n'
                  'end tell')
        subprocess.run(["osascript", "-e", script], capture_output=True)
        notify("dev-cleaner running in Terminal — answer its prompts there")

    def mark_cleaned(self, iid, after):
        tgt = self.targets[iid]
        tgt.after = after
        self.session_freed += tgt.freed
        self.checked.discard(iid)
        name = self.tree.item(iid, "text")[3:]
        vals = list(self.tree.item(iid, "values"))
        vals[1] = fmt_size(after)
        vals[2] = f"-{fmt_size(tgt.freed)}"
        self.tree.item(iid, text=f"✅  {name}", values=vals,
                       tags=("cleaned",))
        self.update_category_row(self.tree.parent(iid))
        self.refresh_dashboard()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
