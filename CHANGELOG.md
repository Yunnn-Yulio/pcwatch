# Changelog

## 0.2.1

- Rewrite the README around the project's purpose, features, download and first run.
- Add a separate usage guide for configuration, log files and troubleshooting.
- Include both guides and the MIT license in the downloadable Python package.
- Recording behavior is unchanged from 0.2.0.

## 0.2.0

- Replace the current C# implementation with a Python standard-library implementation for Windows x64.
- Use English throughout the application, documentation and launcher instructions.
- Preserve local resource samples, filtered events, display/power/session notifications and incident markers.
- Keep rotating logs, bounded retention, persisted event cursors, storage-health reporting and optional per-user startup.
- Provide Python source packaging, SHA-256 checksums and isolated tests in Windows CI.

## 0.1.0

- First public release of the local Windows x64 recorder, written in C#.
- Resource sampling, filtered events, display/power notifications and manual markers.
- Rotating logs, persisted cursors, stop controls and optional automatic startup.
- Visible storage errors and the time of the last successfully saved sample.
- Isolated runtime, storage-recovery, synthetic-privacy and retention tests.
