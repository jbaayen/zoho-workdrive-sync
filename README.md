# zoho-workdrive-sync

Lightweight two-way sync client for [Zoho WorkDrive](https://www.zoho.com/workdrive/) on Linux. Runs in the GNOME system tray, syncs one local folder to one WorkDrive folder, and presents conflicts in a batched resolution dialog.

## Features

- Two-way delta sync (only changed files transferred)
- SHA-256 content hashing for reliable change detection
- Batched conflict resolution dialog (both-modified, modify-vs-delete, etc.)
- System tray icon with sync status
- Filesystem watcher for near-instant local change detection
- Zoho OAuth2 Self Client flow (no browser redirect needed)

## Install

### System dependencies

On Debian/Ubuntu:

```bash
sudo apt install libgirepository-2.0-dev python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
```

### Install and autostart

```bash
./setup.sh
```

This installs the `zoho-workdrive-sync` command into `~/.local/bin` via `uv tool install` and adds a `.desktop` file to `~/.config/autostart/` so it starts automatically on login.

### Install into a virtual environment (alternative)

```bash
uv venv .venv
source .venv/bin/activate
uv pip install .
```

## Setup

1. Create a Self Client at [api-console.zoho.eu](https://api-console.zoho.eu/)
2. Run `zoho-workdrive-sync` -- it will walk you through first-time setup:
   - Enter your client\_id and client\_secret
   - Generate a grant code with scope `WorkDrive.team.READ,WorkDrive.workspace.READ,WorkDrive.teamfolders.READ,WorkDrive.files.ALL`
   - Select your WorkDrive team and folder
   - Choose a local folder to sync

## Usage

```bash
zoho-workdrive-sync
```

The app starts in the system tray. Right-click for options:
- **Sync Now** -- trigger immediate sync
- **Open Sync Folder** -- open the local folder in your file manager
- **Open WorkDrive** -- open Zoho WorkDrive in the browser
- **Quit**

## Sync Logic

Two-way delta sync using a SQLite state database that tracks each file's local SHA-256 hash, mtime, remote etag, and remote modification time.

### Triggers

- **Periodic**: every 300 seconds (configurable via `interval_seconds` in config)
- **Filesystem watcher**: detects local changes via watchdog (debounced 5 seconds)
- **Manual**: "Sync Now" from the tray menu

### Change detection

- **Local**: if the file's mtime changed, recompute the SHA-256 hash. Only mark as changed if the hash differs (avoids false positives from timestamp-only changes).
- **Remote**: mark as changed if the etag or modification time differs from the stored values.

### Action matrix

| Local              | Remote  | Action           |
|--------------------|---------|------------------|
| Added              | —       | Upload           |
| Changed            | —       | Upload           |
| Deleted            | —       | Delete remote    |
| —                  | Added   | Download         |
| —                  | Changed | Download         |
| —                  | Deleted | Delete local     |
| Deleted            | Deleted | Clean up state   |
| Both changed/added | —       | Conflict         |

## Conflict Resolution

When both sides have changed, a GTK dialog shows all conflicts in a table. Per file you choose:

| Resolution      | Effect                                                                                         |
|-----------------|------------------------------------------------------------------------------------------------|
| **Keep Local**  | Upload yours, overwrite remote                                                                 |
| **Keep Remote** | Download theirs, overwrite local                                                               |
| **Keep Both**   | Rename local to `filename (conflict).ext`, download remote version as the original name        |
| **Mark Synced** | Accept current state as baseline without transferring files (useful for pre-synced folders)     |
| **Skip**        | Do nothing this cycle                                                                          |

Bulk buttons ("All Local", "All Remote", "All Both") resolve everything at once.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for module diagram, sync cycle, and threading model.

## License

MIT
