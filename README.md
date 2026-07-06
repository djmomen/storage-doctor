# Storage Doctor 🩺

Deep macOS storage scanner, memory & heat manager with a native Tkinter GUI. No dependencies — pure Python stdlib.

![Risk levels](https://img.shields.io/badge/risk-SAFE%20%7C%20CAUTION%20%7C%20RISKY-blue) ![Platform](https://img.shields.io/badge/platform-macOS-lightgrey) ![Python](https://img.shields.io/badge/python-3.9%2B-green)

## Run

```bash
python3 storage_doctor.py
```

## Three tabs

### 💾 Storage
- **Deep Scan** — sizes space-eaters by category: package-manager caches (brew, npm, pip, uv, bun, yarn, pnpm, go, cargo, Maven, NuGet, .NET, RubyGems, rustup), Xcode junk, container/VM data, browser caches, chat-app media, per-project `node_modules` and Python venvs (searched 6 levels deep), stale installers, files **> 200 MB**, per-app caches > 50 MB, and a generic **Electron/Chromium cache sweep** (Cache / Code Cache / GPUCache / CachedData / Service Worker) across every app in `~/Library/Application Support`.
- **Largest-folders map** — a `du -d 2` sweep surfaces every folder > 1 GB in your home so the scan accounts for *all* used space, not only known caches.
- **Risk-rated** — every item tagged 🟢 SAFE / 🟠 CAUTION / 🔴 RISKY / ⚪ INFO with a plain-English note on what happens if you delete it.
- **Native clean commands** — uses each tool's own cleaner where one exists (`brew cleanup`, `npm cache clean`, `docker system prune`, …) instead of blind `rm`.
- **Deep app uninstall** — removes the bundle **plus all leftovers** from `/` to `~` (Application Support, Caches, Preferences, Logs, Containers, Group Containers, LaunchAgents/Daemons, WebKit, HTTPStorages, saved state, cookies) and installer receipts (`pkgutil --forget`), matched by bundle id + app name.

### 🌡 Heat / CPU
- Live load average, thermal-throttling status (`pmset -g therm`), swap, and a 2-minute CPU graph.
- Top processes grouped by **🟢 SAFE / 🟠 CAUTION / ⚪ SYSTEM** — click a category header to select the whole group, then **Terminate** (SIGTERM → SIGKILL escalation so it actually dies; SYSTEM processes can't be selected).

### 🧠 RAM
- Live memory-in-use gauge (active + wired + compressed), compressor size, and swap.
- Same category selection as Heat — **Free RAM** quits the selected apps, or **Purge inactive** flushes cached memory via `sudo purge`.

## Accuracy

- Sizes use **apparent/logical** bytes (`du -A`) — the number Finder and System Settings show — not physical allocated blocks, which over-report ~5–7% on APFS.
- The dashboard disk gauge reads the **APFS container** total and free (incl. purgeable) via `diskutil`, matching System Settings, instead of `shutil` which undercounts both.
- Before/after sizes are re-measured after cleaning, so "Freed" numbers are real, not estimates.

## Safety rails

- RISKY items (VMs, containers, app uninstalls) require an extra explicit confirmation.
- Terminating/quitting a process warns when CAUTION apps may have unsaved work; SYSTEM processes are never selectable.
- Bulk selection never selects apps for uninstall — apps are only ever removed one by one, on purpose.
- Info-only rows (largest-folders map, personal files) are never deletable.
- Privileged actions use the native macOS admin dialog — no password is ever stored.

## Requirements

macOS, Python 3.9+ (system Python works). Nothing to install.
