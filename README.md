# PCWatch

**A small Windows recorder for the moments before a freeze.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/downloads/windows/)
[![Windows](https://img.shields.io/badge/Windows-x64-0078D4)](#getting-started)
[![Tests](https://github.com/Yunnn-Yulio/pcwatch/actions/workflows/windows.yml/badge.svg)](https://github.com/Yunnn-Yulio/pcwatch/actions/workflows/windows.yml)
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

I built PCWatch after dealing with freezes that left very little to investigate after a reboot. I wanted a record of memory use, busy processes and Windows events leading up to the problem.

PCWatch runs in the system tray and writes that history to local files. It is written in Python, uses the standard library, and keeps collection infrequent so it can stay in the background.

**[Download](https://github.com/Yunnn-Yulio/pcwatch/releases/tag/v0.2.1)** · [Usage guide](docs/usage.md) · [How it works](docs/architecture.md)

## Features

- CPU, RAM, committed memory and paging measurements.
- Disk activity and latency, plus dedicated and shared GPU memory use.
- Process snapshots showing memory and CPU usage, including visible windows flagged as hung.
- Selected Windows events for application crashes, hardware errors, storage problems and unexpected shutdowns.
- Display, sleep and session notifications, with manual incident markers.
- Rotating CSV and JSONL logs, a tray menu and optional startup at sign-in.

## Getting started

Requires **Windows x64** and **64-bit Python 3.11 or newer**. Python is the only dependency.

1. Install [Python](https://www.python.org/downloads/windows/) if needed, with its **Add Python to PATH** option enabled.
2. [Download the Python ZIP](https://github.com/Yunnn-Yulio/pcwatch/releases/download/v0.2.1/PCWatch-0.2.1-python.zip) and extract it into a folder you want to keep.
3. Double-click **`scripts\Run.cmd`**. The PCWatch icon appears in the Windows notification area.

There are no pip packages to install. Normal use does not require administrator access, a service or a driver.

To run it from a terminal, open the extracted folder and enter:

```powershell
python -m pcwatch
```

Alternatively, clone the repository:

```powershell
git clone https://github.com/Yunnn-Yulio/pcwatch.git
cd pcwatch
python -m pcwatch
```

## Using PCWatch

Right-click the tray icon to check **Status**, **Open logs**, **Mark incident now** or **Stop**. Windows may place the icon under the hidden-icons arrow.

| Action | Launcher |
| --- | --- |
| Start recording | `scripts\Run.cmd` |
| Stop recording | `scripts\Stop.cmd` |
| Enable startup at sign-in | `scripts\Enable-startup.cmd` |
| Disable startup and stop | `scripts\Disable-startup.cmd` |
| Open saved logs | `scripts\Open-logs.cmd` |

When a freeze happens, note its approximate time. Once the PC responds, mark the incident and compare the preceding samples with the Windows events. The marker records the time it was clicked; log timestamps are in UTC.

The [usage guide](docs/usage.md) covers custom folders, command-line options, automatic startup and troubleshooting.

## How it works

PCWatch reads Windows system statistics through `ctypes` and native performance counters. A worker collects resource samples every **15 seconds**, and process snapshots and filtered events every **60 seconds**. A hidden message window receives display, power and session notifications.

Everything is saved under **`%LOCALAPPDATA%\PCWatch`**:

```text
PCWatch/
├── Logs/
│   ├── resources-*.csv
│   ├── processes-*.jsonl
│   ├── events-*.jsonl
│   └── activity-*.jsonl
├── status.json
└── event-cursors.json
```

Logs rotate by UTC date and at about **5 MiB** per segment. Cleanup keeps up to **14 days** of files and trims them toward **100 MiB** total. These defaults can be changed from the command line.

PCWatch has no network or upload functionality. It records process names and technical event fields, but excludes window titles, command lines, document contents, full event messages and memory dumps. Review logs before sharing them.

A complete system freeze can stop the recorder too. The logs help reconstruct what happened; a gap alone does not identify the cause.

For the collection APIs, event filters and storage design, see [the architecture guide](docs/architecture.md).

## Development

```powershell
python -m unittest discover -s tests -v
python tools/package.py
```

Tests use temporary data folders. Windows CI runs on Python 3.11 and 3.13. The packaging command creates a source ZIP and SHA-256 checksum under `artifacts/`.

Bug reports and improvements are welcome through [Issues](https://github.com/Yunnn-Yulio/pcwatch/issues) and pull requests. Include the Windows and Python versions, what happened and how to reproduce it.

## License

Released under the [MIT License](LICENSE).

Copyright © 2026 Yunnn-Yulio.
