# workdrive-sync

Lightweight two-way sync client for [Zoho WorkDrive](https://www.zoho.com/workdrive/) on Linux. Runs in the GNOME system tray, syncs one local folder to one WorkDrive folder, and presents conflicts in a batched resolution dialog.

## Features

- Two-way delta sync (only changed files transferred)
- SHA-256 content hashing for reliable change detection
- Batched conflict resolution dialog (both-modified, modify-vs-delete, etc.)
- System tray icon with sync status
- Filesystem watcher for near-instant local change detection
- Zoho OAuth2 Self Client flow (no browser redirect needed)

## Install

```bash
pip install .
```

### System dependencies

On Debian/Ubuntu:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-appindicator3-0.1
```

## Setup

1. Create a Self Client at [api-console.zoho.eu](https://api-console.zoho.eu/)
2. Run `workdrive-sync` -- it will walk you through first-time setup:
   - Enter your client_id and client_secret
   - Generate a grant code with scope `WorkDrive.workspace.READ,WorkDrive.files.ALL`
   - Select your WorkDrive team and folder
   - Choose a local folder to sync

## Usage

```bash
workdrive-sync
```

The app starts in the system tray. Right-click for options:
- **Sync Now** -- trigger immediate sync
- **Open Sync Folder** -- open the local folder in your file manager
- **Open WorkDrive** -- open Zoho WorkDrive in the browser
- **Quit**

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for module diagram, sync cycle, and threading model.

## License

MIT
