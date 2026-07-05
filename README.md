# Storage Doctor 🩺

Deep macOS storage scanner & cleaner with a native Tkinter GUI. No dependencies — pure Python stdlib.

![Risk levels](https://img.shields.io/badge/risk-SAFE%20%7C%20CAUTION%20%7C%20RISKY-blue) ![Platform](https://img.shields.io/badge/platform-macOS-lightgrey) ![Python](https://img.shields.io/badge/python-3.9%2B-green)

## Run

```bash
python3 storage_doctor.py
```

## What it does

- **Deep Scan** — sizes known space-eaters by category: package-manager caches (brew, npm, pip, uv, bun, yarn, pnpm, go, cargo), Xcode junk, container/VM data, browser caches, chat-app media, per-project `node_modules` and Python venvs, stale installers, files > 500 MB, per-app caches > 50 MB.
- **Risk-rated** — every item tagged 🟢 SAFE / 🟠 CAUTION / 🔴 RISKY / ⚪ INFO with a plain-English note on what happens if you delete it.
- **Native clean commands** — uses each tool's own cleaner where one exists (`brew cleanup`, `npm cache clean`, `docker system prune`, …) instead of blind `rm`.
- **Deep app uninstall** — lists every app in `/Applications`; uninstalling removes the bundle **plus all leftovers** from `/` to `~`: Application Support, Caches, Preferences, Logs, Containers, Group Containers, LaunchAgents/Daemons, WebKit, HTTPStorages, saved state, cookies, and installer receipts (`pkgutil --forget`). Matched by bundle id + app name.
- **Live progress window** — every clean run shows a progress bar and a scrolling log (per-item result, bytes freed, failures).
- **Notifications** — macOS banners when admin action is needed, when items fail, and when cleanup finishes.
- **Admin password field 🔑** — optional; kept in memory only, fed to `sudo -S` so privileged cleanups run unattended. Leave empty to get the native macOS admin dialog instead.
- **dev-cleaner integration 🧹** — one click downloads and runs the community [dev-cleaner](https://github.com/jemishavasoya/dev-cleaner) script fully unattended (auto-selects option 1, exits cleanly), streaming its output into the log.

## Safety rails

- RISKY items (VMs, containers, app uninstalls) require an extra explicit confirmation.
- Bulk selection ("Select all", risk chips, category checkboxes) never selects apps for uninstall — apps are only ever removed one by one, on purpose.
- Info-only rows (Pictures, Documents, …) are never deletable.
- Before/after sizes are re-measured, so "Freed" numbers are real, not estimates.

## Requirements

macOS, Python 3.9+ (system Python works). Nothing to install.
