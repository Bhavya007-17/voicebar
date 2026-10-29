# Stage 8 operations work — 2026-10-22

The operations changes provide bounded decision logs, transcript removal, local
readiness diagnostics, portable first-run configuration and validated snapshots
for live configuration reload. This file records the component's checked API
choices; the full stage gate is tracked separately and has not passed yet.

## Checked choices for docs/DECISIONS.md

- The environment reported Python **3.12.13**, uv **0.11.21**, PyYAML **6.0.3**
  and sounddevice **0.5.3**. The installed modules and existing project call
  sites were inspected before writing these changes. No dependency was added
  for operations.
- The microphone probe uses the installed `query_devices(kind="input")`,
  `check_input_settings`, `RawInputStream`, `start` and
  `close(ignore_errors=False)` signatures. These are the same format and
  lifecycle used by `DefaultInput`: 16 kHz, mono signed 16-bit frames. Opening
  and starting a stream detects permission/device failures that enumeration
  alone cannot detect. Probe samples are discarded and never saved.
- Model diagnostics reuse `model_assets` and manifest-backed
  `require_model(..., verify_hashes=True)`. The shared segmentation asset is
  also required. Diagnostics never import a downloader to repair missing files.
- Shortcut diagnostics reuse `parse_hotkey` and `WindowsHotkey.register/close`,
  holding all registrations simultaneously to detect conflicts. When enabled,
  the Copilot hook is installed and immediately released too. A running
  Voicebar may already own the shortcuts; the diagnostic reports that conflict
  and asks the user to quit it before rechecking. It does not claim ownership
  from an unverified status file. Physical Copilot-key delivery remains a
  separate manual check.
- Optional resolver diagnostics reuse the existing bounded
  `OllamaBackend.local_model(timeout_s=1.5)`. Its client host is fixed to
  `127.0.0.1`, and metadata must identify a local permitted model. Disabled
  resolver configuration makes no connection at all.
- Default YAML is written through the already installed `yaml.safe_dump`, with
  exclusive file creation so ordinary initialization never replaces an existing
  config file. Explicit `init --portable` moves the old config directory intact
  to `.cache/config-backups/` before writing defaults. Speech selection remains
  unset until the user tests and selects a backend. Pinned apps start empty;
  startup feedback explains how to fill them.
- Configuration polling fingerprints local YAML content, validates the complete
  catalog, gate, UI, executor, speech selection, logging policy and resolver
  settings, and publishes a result only if its fingerprint remains unchanged.
  Malformed saves preserve the last validated generation. Snapshot-based gate,
  UI and logging validators accept an optional `document` so application of a
  validated result need not reread a later disk generation. Polling belongs on
  a worker and does not perform microphone, model or socket initialization.
- The JSON-lines writer caches policy by settings-file modification time and
  byte size. It does no YAML parse on an unchanged per-utterance write. It rotates
  before a record would exceed `max_file_bytes`, drops transcript payloads from
  oversized records, and removes the oldest files directly in `logs/` until the
  total cap is met. Symlinks and directories are not deleted. Decisions and
  numeric timings survive normal transcript removal.
- `logs.keep_transcripts: false` removes speech, text/query/URL payloads, model
  prompts and diagnostic echoes recursively. Invalid logging settings select
  transcript removal until valid settings return. This affects new records;
  old logs and WAV recordings remain. The bar's live feedback is independent.
  `voicebar logs --last 50` reads complete JSON objects across sessions, ignores
  an incomplete tail and orders interleaved session records by their timestamp.

## Verified

Focused component verification on 2026-10-22:

```text
.venv\Scripts\python.exe -m pytest tests/test_operations_stage8.py -q --basetemp C:\dev\voicebar\.cache\pytest-operations-4
...............                                                          [100%]
15 passed in 1.26s

.venv\Scripts\ruff.exe check voicebar/logs.py voicebar/configuration.py voicebar/doctor.py tests/test_operations_stage8.py
All checks passed!
```

The tests cover defaults preserving existing files, exact portable backups,
privacy across plans/results/diagnostics, per-file rotation and total retention
including old reports, live privacy changes, malformed saves, interleaved log
ordering, invalid config generations and later recovery, alias publication,
independent diagnostic failures, absent model selection and a disabled resolver
making no connection. They use temporary local data and fake diagnostic probes;
they open no real microphone or external app.

## Not verified by this component run

- The full `uv run pytest` stage gate and runtime integration are separate.
- Actual microphone access, a physical Copilot press, hotkey delivery, startup
  sign-in, app-device reconnection and real desktop actions were not exercised.
- No model inference, adapter change, live resolver call or network socket proof
  was run by the operations component tests.
- Fresh-clone startup and before/after saved-recording latency measurements
  remain full-stage checks. README describes the required steps without treating
  them as a completed demonstration.

The first uv attempt used a sandbox cache directory that denied writes. With
`UV_CACHE_DIR` placed inside the project, uv attempted to refresh its editable
entry point but the existing running app held `voicebar.exe`. Component tests
therefore used the same environment's Python directly and a workspace-local
pytest temporary directory. These environment failures are not counted as a
passed uv gate, and no existing user app was closed for this component test.
