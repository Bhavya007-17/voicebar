# Stage 9 gate — passed (2026-10-24)

This gate covers the implemented command-session/UI/context changes described
in [STAGE9-STRUCTURE.md](STAGE9-STRUCTURE.md). It does not certify universal
hands-free operation, a visual model, or an end-to-end latency improvement.

## Verified

```text
.venv\Scripts\python.exe -m pytest --basetemp .cache/pytest-stage9-native-gate -q
2298 passed, 1 skipped in 48.80s
```

Full output: [stage9-gate-native-pytest.txt](evidence/stage9-gate-native-pytest.txt).
This run was approved outside the restricted sandbox, with the unchanged native
tests still requiring ownership of every window they act on. The existing skip
is the second-monitor test on this one-monitor setup. No threshold was lowered,
test skipped by command line, or delay added to mask a race.

- New fake tests cover two-minute expiry, wake activation, split searches,
  cancellation, authorized local fallback, screen ambiguity, revocation during
  resolution, bound foreground identity, local app-memory behavior, Calculator
  key sequences, browser recency and browser-tab ancestry.
- Clear tab/navigation/URL commands are tested to make no UIA query.
- Existing native Notepad/Calculator and focus tests passed. These do not prove
  the new calculation result, browser-tab selection or pill compositing live.
- The full gate includes the existing tier-1 parser p95-under-5-ms benchmark.
- Ruff passed on every Stage 9 Python source and test file.
- Hidden Qt renders were inspected for layout/clipping:
  [idle](evidence/stage9-pill-idle.png),
  [expanded](evidence/stage9-pill-expanded.png).
- Immutable files match their pre-edit SHA-256 values:
  - parser contract: `DED62BA9134934237BBB2C2E5FE2EE064503AAA7285AC61A9B1A7E43DCD943EB`
  - gate contract: `60AB126F07314276D835E7308449A34081B4A26D3B579F0F9EA93FF65FFBA8CE`
  - `gate.py`: `C8B80E3D2269AA4E0141DB97540A9C838E251D12BA1FBBB9400D561623206CA5`

## Initial gate and correction

The sandboxed run reported `4 failed, 2276 passed, 19 errors in 125.65s`.
Its output is preserved in [stage9-gate-pytest.txt](evidence/stage9-gate-pytest.txt).
One failure exposed a legacy compatibility regression: foreground detection ran
without configured app actions even when conversation mode was disabled. The
product guard was restored, and the unchanged inspection/browser tests passed
(`55 passed in 0.51s`). The other failures involved Windows endpoint-table
access denied, process-cleanup observation and unconfirmed owned Notepad
launches. The full approved native rerun above passed without relaxing them.

## Not verified / not implemented

- Live microphone accuracy or lower end-of-speech-to-action latency. Recognition
  backend/model settings are unchanged; zero latency is not achieved.
- New Calculator arithmetic and new named-browser-tab behavior on actual app
  providers. These have fake coverage; no user browser was exercised.
- Native pill compositing/focus on physical displays, mixed DPI or live style
  changes. The alpha material is not a native blur effect.
- Local screenshot/vision inference, semantic assignment selection, arbitrary
  workflows, automatic learned commands or a browser-wide MRU history.
- A new physical network-disconnected run. Existing runtime network tests
  passed; no new cloud API or network transport was added.
- Reloading the currently running Voicebar process with these Python changes.
  Restart Voicebar before using the new behavior.

## Commit scope

The checkout already contained unfinished Stage 8 runtime work and had a clean
Git index before this task. Stage 9 calls into that runtime's validated settings,
reload, logging and lifecycle APIs. Committing only the new files or overlapping
hunks would leave missing modules or untested alternate source.

The Stage 9 commit therefore explicitly includes the pending runtime/config/
dependency prerequisites and their regression tests, including the Stage 8
offline helper. Its source matches the working tree that passed this gate.
This is not a declaration that the separate Stage 8 physical/offline acceptance
criteria passed. Unrelated historical experiments, temporary folders and older
reports remain outside this commit. The pre-task source/index snapshot is
retained locally in `.cache/stage9-baseline`.
