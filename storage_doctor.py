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
import shlex
import subprocess
import tempfile
import threading
import shutil
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont

HOME = os.path.expanduser("~")

# ---------------------------------------------------------------- helpers

def du_bytes(path: str) -> int:
    try:
        out = subprocess.run(
            ["du", "-sk", path], capture_output=True, text=True, timeout=600
        )
        return int(out.stdout.split()[0]) * 1024
    except Exception:
        return 0


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

    # -- python virtualenvs --
    progress_cb("Hunting Python venvs...")
    try:
        out = subprocess.run(
            ["find", HOME, "-maxdepth", "4", "-type", "d",
             "(", "-name", ".venv", "-o", "-name", "venv", ")",
             "-prune", "-not", "-path", f"{HOME}/Library/*"],
            capture_output=True, text=True, timeout=120)
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
    progress_cb("Hunting files > 500 MB (this takes a moment)...")
    try:
        out = subprocess.run(
            ["find", HOME, "-xdev", "-type", "f", "-size", "+500M",
             "-not", "-path", "*orbstack*",
             "-not", "-path", f"{HOME}/.colima/*",
             "-not", "-path", f"{HOME}/Library/Containers/com.docker.docker/*"],
            capture_output=True, text=True, timeout=180)
        for p in out.stdout.strip().splitlines():
            tgt = Target("Huge Files (>500 MB)", p.replace(HOME, "~"), p,
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
            ["find", HOME, "-maxdepth", "4", "-name", "node_modules",
             "-type", "d", "-prune", "-not", "-path", f"{HOME}/Library/*"],
            capture_output=True, text=True, timeout=120)
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
    apps_dir = "/Applications"
    if os.path.isdir(apps_dir):
        for f in sorted(os.listdir(apps_dir)):
            if not f.endswith(".app") or f.startswith("."):
                continue
            p = os.path.join(apps_dir, f)
            found.append(Target(
                "Applications (deep uninstall)", f[:-4], p, RISK_RISKY,
                "removes app + ALL leftovers (caches, prefs, containers, "
                "launch agents, receipts) — from / to ~",
                ("uninstall", p)))

    progress_cb("Mapping storage hogs...")
    for d in ("Pictures", "Desktop", "Documents", "Movies", "Music"):
        p = os.path.join(HOME, d)
        if os.path.isdir(p):
            found.append(Target("Storage Hogs (info only)", f"~/{d}", p,
                                RISK_INFO, "personal files — review manually",
                                ("info", None)))
    return found


# ---------------------------------------------------------------- cleaning

def sudo_shell(cmd: str, pw, timeout=600):
    """Run a shell command as root.

    With a stored password -> non-interactive `sudo -S`.
    Without one -> native macOS admin dialog (and a notification so the
    user knows action is required).
    """
    if pw:
        r = subprocess.run(["sudo", "-S", "-p", "", "sh", "-c", cmd],
                           input=pw + "\n", capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0 and "try again" in r.stderr.lower():
            return False, "wrong admin password"
        return r.returncode == 0, (r.stderr.strip()[:200] or "ok (admin)")
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


def uninstall_app(app_path: str, pw, log):
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
        "rm -rf " + " ".join(shlex.quote(p) for p in root_paths), pw)
    if not ok:
        return False, f"admin removal failed: {msg}"
    if bid:  # forget installer receipts too
        sudo_shell(f"pkgutil --pkgs | grep -F {shlex.quote(bid)} | "
                   f"xargs -n1 pkgutil --forget 2>/dev/null || true", pw,
                   timeout=60)
    log(f"   removed {1 + len(leftovers)} paths")
    return True, f"uninstalled ({len(leftovers)} leftovers swept)"


def clean_target(tgt: Target, pw=None, log=lambda m: None):
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
                              f"-maxdepth 1 -exec rm -rf {{}} +", pw,
                              timeout=300)
        if kind == "uninstall":
            return uninstall_app(arg, pw, log)
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
        self.pw_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.apply_filter())

        self._build_dashboard()
        self._build_toolbar()
        self._build_tree()

        self.status = ttk.Label(root, text="Ready — click  🔍 Deep Scan",
                                padding=(10, 6))
        self.status.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(root, mode="indeterminate")

        self.refresh_dashboard()
        self.root.after(120, self.poll)

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
            u = shutil.disk_usage(HOME)
            pct = u.used / u.total
            self.disk_label.config(
                text=f"Disk  {fmt_size(u.used)} used of {fmt_size(u.total)}")
            self.disk_sub.config(
                text=f"{fmt_size(u.free)} free  ·  {pct:.0%} full")
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
    def _build_toolbar(self):
        top = ttk.Frame(self.root, padding=(10, 8))
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
        ttk.Label(top, text="🔑 Admin pw:").pack(side="left", padx=(14, 2))
        ttk.Entry(top, textvariable=self.pw_var, show="•", width=13
                  ).pack(side="left")
        self.btn_clean = ttk.Button(top, text="🗑  Clean selected…",
                                    command=self.confirm_clean, state="disabled")
        self.btn_clean.pack(side="right")
        ttk.Entry(top, textvariable=self.search_var, width=22
                  ).pack(side="right", padx=8)
        ttk.Label(top, text="Filter:").pack(side="right")

        # clickable risk chips — click one to select everything at that risk
        chips = tk.Frame(self.root, padx=10)
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
    def _build_tree(self):
        wrap = ttk.Frame(self.root)
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

    # ---------- scanning ----------
    def scan(self):
        self.btn_scan.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.targets.clear()
        self.checked.clear()
        self.refresh_dashboard()
        self.progress.pack(side="bottom", fill="x")
        self.progress.start(10)
        threading.Thread(target=self._scan_worker, daemon=True).start()

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
                    self.progress.stop()
                    self.progress.pack_forget()
                    self.btn_scan.config(state="normal")
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
        pw = self.pw_var.get().strip() or None
        log = lambda m: self.q.put(("log", m))
        needs_sudo = any(self.targets[i].clean[0] in
                         ("sudo_rm_contents", "uninstall") for i in iids)
        if needs_sudo and not pw:
            notify("Some items need admin rights — watch for the "
                   "password dialog")
        failed = []
        for i, iid in enumerate(iids, 1):
            tgt = self.targets[iid]
            head = f"[{i}/{len(iids)}] {tgt.name}"
            self.q.put(("cprog", (i - 1, head)))
            self.q.put(("status", f"Cleaning  {tgt.name} …"))
            log(f"→ {tgt.name}  ({fmt_size(tgt.size)})")
            ok, msg = clean_target(tgt, pw, log)
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
                "Download and run the community dev-cleaner.sh script?\n"
                "(auto-selects option 1, exits with 0)"):
            return
        self.btn_dev.config(state="disabled")
        self.btn_clean.config(state="disabled")
        self._batch_start = self.session_freed
        self.clean_win = CleanWindow(self.root, 1)
        threading.Thread(target=self._dev_cleaner_worker, daemon=True).start()

    def _dev_cleaner_worker(self):
        log = lambda m: self.q.put(("log", m))
        try:
            path = os.path.join(tempfile.gettempdir(), "dev-cleanup.sh")
            log(f"Downloading {DEV_CLEANER_URL} …")
            r = subprocess.run(["curl", "-fsSL", DEV_CLEANER_URL, "-o", path],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                log(f"❌ download failed: {r.stderr.strip()[:200]}")
                notify("dev-cleaner download failed — check network")
                return
            os.chmod(path, 0o755)
            pw = self.pw_var.get().strip()
            if pw:
                # prime sudo timestamp so the script's internal sudo calls pass
                subprocess.run(["sudo", "-S", "-p", "", "-v"],
                               input=pw + "\n", capture_output=True,
                               text=True, timeout=30)
            else:
                notify("dev-cleaner may need admin rights — add your "
                       "password in the 🔑 field to run unattended")
            log("Running dev-cleaner.sh (answers: 1, then 0 to exit) …\n")
            self.q.put(("cprog", (0, "dev-cleaner running…")))
            proc = subprocess.Popen(
                [path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            try:
                # ponytail: option 1 once, then 0s so any repeated menu exits
                proc.stdin.write("1\n" + "0\n" * 10)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            for line in proc.stdout:
                log(line.rstrip())
            proc.wait(timeout=1800)
            log(f"\ndev-cleaner exited with code {proc.returncode}")
            notify("dev-cleaner finished")
        except Exception as e:
            log(f"❌ dev-cleaner error: {e}")
            notify("dev-cleaner failed — see log")
        finally:
            self.q.put(("clean_done", None))
            self.q.put(("status", "dev-cleaner finished."))

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
