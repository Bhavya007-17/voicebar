# Stage 8 gate — incomplete (2026-10-23)

The stage is **not passed and is not committed as a passed gate**. The user
declined the physical offline test. The adaptive candidate was slower, and
the complete test suite's latest run has a Windows file-access failure.

## Verified

- Copilot toggle code uses the physical Win+LeftShift+F23 chord, with the
  existing Ctrl+Alt+F9 fallback. Exact-chord tests and native hook install/close
  passed. The user physically confirmed microphone live then closed on two
  Copilot presses. The updated app was restarted at the user's request through
  windowless Python and is left with the microphone closed.
- **Start with Windows is enabled**, targeting the project's `pythonw.exe`.
  The same setter is used by the tray. Actual sign-in has not been simulated.
- Selected Qwen/CUDA GUI socket replay: **4/4 expected decisions**, **1,309
  samples**, **69.51 s**, zero forbidden endpoints/listeners/audit attempts.
  Maximum actual sample gap **101.78 ms**. Execution is recorded rather than
  sending fixture commands to other apps; polling has documented limitations.
- Dependency review covered **61 distributions / 9,762 Python files**; telemetry,
  update checks and model-download paths are documented in `STAGE8-NETWORK.md`.
- Warm local model/catalog reuse, validated config snapshots/model replacement,
  log rotation/size caps/privacy, `logs --last 50`, `doctor`, default config,
  microphone-menu refresh, windowless startup and README changes are implemented.
- A corrected clean checkout with its own environment and downloaded models
  passed doctor and reached an actual Ready/Off app with real global hotkeys,
  then closed cleanly. Final ready time **3.062 s** using the README CPU setup.
  See `STAGE8-FRESH-CLONE.md` and `evidence/stage8-fresh-app-final.json`.
- Resilience tests use owned fake resources/native handles. Hardware/manual
  limits and exact evidence are in `STAGE8-RESILIENCE.md`.

## Test gate output

```text
uv run pytest
1 failed, 2133 passed, 1 skipped in 69.02s (0:01:09)
FAILED tests/test_windows_integration.py::test_notepad_identity_foreground_and_placement
PermissionError: [WinError 5] Access is denied:
.cache/test-ownership-...json....tmp -> .cache/test-ownership-...json
```

Complete latest output: `evidence/stage8-gate-pytest-quiescent.txt`.
The existing one-monitor skip was retained. No test was deselected, threshold
lowered, contract expectation changed or sleep added to mask a failure.

Earlier complete runs are retained:

- `stage8-gate-pytest.txt`: 2,129 passed, 1 skipped, one missing-model preflight
  import regression. Fixed product ordering so telemetry opt-out cannot import
  a native runtime before model validation; the original expectation is unchanged.
- `stage8-gate-pytest-final.txt`: 2,132 passed, 1 skipped, one existing 20 ms
  resolver deadline failure. Its unchanged isolated test passed, and 100
  instrumented repetitions passed; the original failure is not certified away.
  See `STAGE8-RESOLVER-DIAGNOSTIC.md`. No resolver deadline/source was changed.

Python lint and generated-command checks passed on the final changed files.
Parser/gate contract identity is recorded separately.

## Final latency numbers

Historical 348 decision-log samples (mixed use, not matched recordings):

| Hop | p50 / p95, ms |
| --- | ---: |
| End-of-speech wait | 320.00 / 320.00 |
| Recognition | 993.80 / 4169.09 |
| Gate plus parse | 0.06 / 13.73 |
| Actual execute start | unavailable |

Matched **134 raw personal WAVs**, configured Qwen/CUDA:

| Measured quantity | Before p50 / p95, ms | Experimental adaptive p50 / p95, ms |
| --- | ---: | ---: |
| Sample-clock endpoint wait | 54.00 / 320.00 | 48.00 / 320.00 |
| Recognition, including provisional work | 1545.66 / 3845.99 | 1618.75 / 4490.53 |
| Gate plus parse | not recorded | not recorded |
| Actual execute start | not executed | not executed |

The endpoint rows are heavily limited by recording EOF and **are not live
endpoint latency**. There were **zero changed final transcripts and zero new
early boundaries**. Only 26 previews and 11 accepted complete partials exercised
the policy; protected free text was not exercised by those accepted previews.
Keep adaptive mode disabled. This does not establish a live no-cutoff claim.

Actual startup warmup: **8,101.79 ms** before Ready. The same first command then
took **1,288.84 ms**, versus **4,984.23 ms** without warmup; repeat **1,248.38 ms**.
This moves cold work into startup; it does not establish a steady-state speedup
or the 400 ms target. Generation dominated the retained cumulative profile.

New runtime logs distinguish dispatch, native action-start receipts and action
completion. The socket/recording tests deliberately execute no native action,
so absent native-start receipts remain unavailable instead of invented numbers.

## Not verified / blocked

- Physical networking-off run: **declined by the user**; no adapter changed.
- Green complete pytest gate: latest Windows ownership-record replacement failed.
- Live adaptive endpoint latency improvement, protected text/dictation pause
  acceptance, full matched four-hop before/after measurements and the 400 ms target.
- Real Bluetooth/unplug/default-device notification, physical suspend/lock
  cycles and sign-in startup. Tests/manual procedures are
  described separately; those procedures are not represented as performed.
- The real restarted bar's Qt accessibility tree exposes only its window name;
  external UI inspection did not verify its Ready/Off labels. The owned fresh
  app and selected-model runtime tests verified those states directly, and the
  user physically confirmed the running app toggles capture and closes it.

Firewall commands were printed only. The uv venv executable is a launcher;
the actual shared interpreter also needs a rule for an interpreter-level block,
which would affect other projects using it. A blanket outbound rule also blocks
the optional loopback resolver. See the exact paths/caveats in `STAGE8-NETWORK.md`.
