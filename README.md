# PCWatch

A local Windows recorder for investigating freezes, memory pressure and display or wake problems. **Version 0.2.0 is written in Python and uses only the standard library.** Windows x64, Python 3.11 or newer, MIT license.

PCWatch saves measurements before a problem so you can compare them with Windows events afterward. It does not automatically diagnose a faulty component, run stress tests or change power settings.

## Quick start

1. Install **64-bit Python 3.11 or newer** from [python.org](https://www.python.org/downloads/windows/) if it is not already installed. Enable the installer option that adds Python to PATH.
2. Download the Python ZIP from [Releases](../../releases), then extract it into a folder you want to keep. You can also clone this repository.
3. Double-click **`scripts\Run.cmd`**. A PCWatch icon appears in the notification area; Windows may put it in the hidden-icons menu.
4. Right-click the icon to view status, open logs, mark an incident or stop the monitor.

No `pip install`, administrator access, service or driver is required. Keep the extracted files together. The package includes Python source, not a standalone executable; Python must remain installed.

To launch from a terminal in the extracted folder:

```powershell
python -m pcwatch
```

For a console session without the tray icon:

```powershell
python -m pcwatch --no-tray
```

## Everyday use

| Action | Launcher |
|---|---|
| Start the monitor | `scripts\Run.cmd` |
| Stop the monitor; keep its logs | `scripts\Stop.cmd` |
| Start automatically when you sign in | `scripts\Enable-startup.cmd` |
| Remove automatic startup and stop the monitor | `scripts\Disable-startup.cmd` |
| Open the log folder | `scripts\Open-logs.cmd` |

Automatic startup is optional. It creates a shortcut in your user account's Startup folder pointing to this copy of PCWatch and `pythonw.exe`. Enable it after choosing the folder where you will keep the files. Before moving that folder or replacing your Python installation, disable startup from the existing copy. After the change, enable startup from the new copy. PCWatch protects a shortcut that belongs to a different copy instead of silently replacing it.

The equivalent commands are:

```powershell
python -m pcwatch --stop
python -m pcwatch --install-startup
python -m pcwatch --remove-startup
```

When a freeze occurs, note the approximate time. After the computer responds or restarts, use **Mark incident** in the tray menu and inspect the samples before that time. UTC timestamps end in `Z`; convert your local incident time to UTC when comparing records. Review any logs before sharing them.

## What it records

| Frequency | Data |
|---|---|
| Every 15 seconds | Total CPU, available and used RAM, committed memory and its limit, system pools, paging, disk queue/latency/traffic, aggregate dedicated/shared GPU memory, and the monitor's own resource use |
| Every 60 seconds | Accessible processes ranked by private memory and CPU, visible windows Windows identifies as hung, and selected technical events |
| When notified by Windows | Display on/off/dim, session lock/unlock, suspend/resume, and manual incident markers |

The default data folder is **`%LOCALAPPDATA%\PCWatch`**. Inside it:

- `Logs/resources-*.csv`: resource samples, suitable for spreadsheet tools.
- `Logs/processes-*.jsonl`: process snapshots.
- `Logs/events-*.jsonl`: filtered Windows events.
- `Logs/activity-*.jsonl`: lifecycle, display/power changes and sampling gaps.
- `status.json`: current status and the last successfully saved sample.
- `event-cursors.json`: progress through the Windows event logs.

JSONL files contain one JSON object per line. All timestamps use UTC. Logs rotate at **5 MiB** per segment. At the default 60-second details interval, PCWatch removes its own log files that have not been updated for **14 days** and trims its own logs toward **100 MiB** total. A small temporary excess is possible between cleanup passes; reaching the size cap shortens the retained history. Writes are flushed to disk, but a sudden shutdown can still lose the newest data.

## Options

Run `python -m pcwatch --help` for the complete command-line help.

| Option | Default | Purpose |
|---|---|---|
| `--data-dir PATH` | `%LOCALAPPDATA%\PCWatch` | Choose a separate data folder |
| `--interval SECONDS` | `15` | Resource sampling interval |
| `--details SECONDS` | `60` | Process and event collection interval |
| `--max-log-mb MIB` | `100` | Total log storage target |
| `--segment-kb KIB` | `5120` | Maximum log segment size |
| `--retention-days DAYS` | `14` | Age limit based on each file's last update |
| `--max-seconds SECONDS` | `0` | Stop after a duration; `0` keeps running |
| `--no-tray` | Off | Run without a notification-area icon |
| `--cleanup-only` | Off | Apply retention and exit |

For a short recording in a separate folder:

```powershell
python -m pcwatch --no-tray --data-dir .\recording --max-seconds 120
```

Use the same `--data-dir` with `--stop` to stop that instance. One monitor can run per data folder. The default sampling intervals keep collection infrequent; reducing them increases overhead and log volume.

## Troubleshooting

- If `python` is not recognized but the Python launcher is installed, replace `python` with `py -3` in the commands above. Otherwise, reinstall Python with its PATH option enabled.
- If no tray icon appears, check Windows' hidden notification icons. To see a startup error, run `python -m pcwatch --no-tray --max-seconds 30` from the extracted folder. A failed startup can also leave technical error metadata in `%LOCALAPPDATA%\PCWatch\last-error.json`.
- If **Status** reports a storage error, check the data folder's write access and available disk space, then restart PCWatch. Logs and status show which component failed; inaccessible event channels or missing counters may limit collection even when log storage is healthy.

## Privacy and limits

PCWatch has no network or upload functionality. It does not record documents, keystrokes, passwords, clipboard contents, screenshots, camera/audio, web history, window titles, process command lines or paths, complete event messages, or memory dumps. **Process names reveal which programs were running.**

Event fields use a technical allowlist. WHEA/CPER records retain a small header summary and section types, excluding raw payloads and unique record identifiers. PCWatch reports inaccessible logs or processes as limited coverage rather than changing permissions. Privacy tests use synthetic markers; they do not certify every possible Windows event or make actual logs safe to publish automatically.

- A complete system freeze can stop PCWatch too. A gap means sampling was interrupted; it does not establish why.
- High RAM use does not prove a memory shortage. Process working sets include shared pages and should not be added to estimate total RAM usage.
- Paging activity alone does not prove RAM exhaustion. GPU memory use is not GPU load; temperature and GPU utilization are not measured.
- Missing counters remain unavailable. Permissions, drivers and Windows configuration affect collection coverage.
- A storage failure is reflected in status and the time of the last successful sample. An old sample should not be treated as current.
- Overhead depends on the machine, accessible processes and collection intervals. The Python version uses native Windows APIs through `ctypes`, sleeps between collection passes and runs below normal priority; there is no universal RAM or CPU guarantee.

## Development

From a repository checkout:

```powershell
python -m unittest discover -s tests -v
python tools/package.py
```

Tests use isolated temporary data folders. They do not enable startup or modify the operational log folder. The package and its SHA-256 checksum are written into `artifacts`, which Git ignores. Windows CI runs the tests on Python 3.11 and 3.13.

## Source guide

| Module | Responsibility |
|---|---|
| `pcwatch/app.py` | Options, collection schedule, status and application lifecycle |
| `pcwatch/windows.py` | Windows integration, single-instance controls, tray and power/session notifications |
| `pcwatch/startup.py` | Native per-user Startup shortcut management |
| `pcwatch/metrics.py` | Native CPU/RAM/process measurements and PDH counters |
| `pcwatch/events.py` | Filtered Windows events, persisted cursors and privacy allowlists |
| `pcwatch/storage.py` | CSV/JSONL writes, rotation, flushing and retention |

Read [the architecture and Python design choices](docs/architecture.md). The current implementation is Python; version 0.1.0 in the release history was the earlier C# implementation.
