# Architecture

## Overview

workdrive-sync is a two-way file sync client between a local folder and a Zoho WorkDrive folder. It runs as a GNOME system tray application.

## Module Diagram

```mermaid
graph TD
    main[main.py<br/>Entry point & App class]
    tray[tray.py<br/>System tray icon & menu]
    sync[sync.py<br/>Two-way sync engine]
    conflicts[conflicts.py<br/>GTK conflict dialog]
    api[api.py<br/>WorkDrive REST API]
    auth[auth.py<br/>Zoho OAuth2]
    state[state.py<br/>SQLite state DB]
    config[config.py<br/>Config & token files]
    watchdog[watchdog<br/>FS event observer]

    main --> tray
    main --> sync
    main --> watchdog
    sync --> api
    sync --> state
    sync --> conflicts
    api --> auth
    auth --> config
    state --> config
    main --> config
```

## Sync Cycle

```mermaid
sequenceDiagram
    participant Timer as Sync Timer / Watchdog
    participant Engine as SyncEngine
    participant Local as Local FS
    participant DB as SQLite State
    participant API as WorkDrive API
    participant Dialog as Conflict Dialog

    Timer->>Engine: trigger sync
    Engine->>Local: walk local folder
    Engine->>DB: load known state
    Engine->>API: walk remote folder
    Engine->>Engine: classify changes

    alt Non-conflicting changes
        Engine->>API: upload / download / delete
        Engine->>Local: write / delete files
        Engine->>DB: update state records
    end

    alt Conflicts detected
        Engine->>Dialog: show conflict list
        Dialog-->>Engine: user resolutions
        Engine->>API: apply resolutions
        Engine->>DB: update state records
    end
```

## Delta Detection

Each file is tracked with both local (mtime + SHA-256 hash) and remote (etag + modified_time) state. On each sync cycle:

```mermaid
flowchart TD
    Start([Scan]) --> ScanLocal[Walk local folder]
    Start --> ScanRemote[Query WorkDrive API]

    ScanLocal --> CompareLocal{mtime changed?}
    CompareLocal -->|Yes| HashCheck{hash changed?}
    CompareLocal -->|No| Unchanged[local unchanged]
    HashCheck -->|Yes| LocalMod[local_modified]
    HashCheck -->|No| Unchanged

    ScanRemote --> CompareRemote{etag or modified_time changed?}
    CompareRemote -->|Yes| RemoteMod[remote_modified]
    CompareRemote -->|No| Unchanged2[remote unchanged]

    LocalMod --> Classify
    RemoteMod --> Classify
    Unchanged --> Classify
    Unchanged2 --> Classify

    Classify{Both changed?}
    Classify -->|No| AutoSync[Auto sync<br/>upload / download / delete]
    Classify -->|Yes| Conflict[Add to conflict list]
```

## Conflict Resolution

| Local | Remote | Conflict Type |
|-------|--------|---------------|
| modified | modified | Both modified |
| added | added | Both added |
| modified | deleted | Modified locally, deleted remotely |
| deleted | modified | Deleted locally, modified remotely |

User resolves each conflict via a batched GTK dialog with options:
- **Keep Local** -- upload local version (or delete remote if local was deleted)
- **Keep Remote** -- download remote version (or delete local if remote was deleted)
- **Keep Both** -- rename local to `file (conflict).ext`, download remote
- **Skip** -- do nothing this cycle

## File Layout

```
~/.config/workdrive-sync/
    config.json      # client_id, client_secret, folder paths, team_id
    token.json       # OAuth refresh token (chmod 600)
    state.db         # SQLite sync state (per-file hashes, etags)
```

## Threading Model

```
GTK Main Thread          Background Sync Thread       Watchdog Thread
      |                         |                          |
      |--- tray menu events     |                          |
      |--- conflict dialog      |                          |
      |                         |--- periodic sync         |
      |                         |    (every N seconds)     |
      |                         |                          |--- FS events
      |                         |<-- debounced trigger ----|
      |<-- GLib.idle_add -------|                          |
      |    (conflict dialog)    |                          |
```

- **GTK main thread**: runs the event loop, handles tray clicks and conflict dialogs
- **Sync thread**: periodic sync loop, also triggered by watchdog or manual "Sync Now"
- **Watchdog thread**: monitors local folder for changes, debounces (5s) before triggering sync
