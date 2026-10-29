# Stage 5 — gate passed

The bar, tray, global hotkeys and `voicebar app` entry point are implemented.
On 2026-10-12 the user reported "everything works" after the four requested
hands-on checks. The unchanged full suite then passed. This closes the Stage 5
gate; Stage 6 was not started.

Latest exact result, saved in
[stage5-pytest.txt](evidence/stage5-pytest.txt):

```text
uv run pytest
============================ 1274 passed in 29.53s ============================
```

No tests were skipped. The physical second-monitor integration check ran on
this machine during this rerun. No implementation, test, threshold or delay
was changed between the prior failed gate and this passing rerun.

## Verified

- Latest `uv run pytest`: 1,274 passed in 29.53 seconds, including all new
  Stage 5 tests, both contracts and the real desktop integration checks.
- Native Windows focus test: open an owned Notepad, show/expand the bar,
  physically click the bar and its Listening button, collapse it, and assert
  that the same Notepad stays foreground throughout. Native extended styles
  exclude the bar from taskbar/Alt+Tab. No keyboard input is sent to Notepad.
- Deterministic dwell cancellation, delayed expansion, idle collapse, live
  microphone colours, persistent dictation indication during work, plain raw
  transcript text, two nearest candidates, and configured-display fallback
  and recovery using logical screen geometries.
- Queued controller controls; actual adapter closure logging; stale speech
  rejection on Off/mode/device/lock; held-capture arbitration, silent-hold
  refusal, timeout cancellation/poisoning, and simulated suspend/lock restore.
- Hotkey registration/release/collision/cleanup, per-session single-instance
  exclusion, this-session Windows notifications, and real native COM shortcut
  save/load in a test-owned directory. No real Startup shortcut was enabled.
- Real desktop/model/microphone smoke through the CLI app entry point:
  [stage5-app-smoke.json](evidence/stage5-app-smoke.json). All actions were
  forced to dry run. The existing local model loaded, all hotkeys registered,
  32,256 callback samples were captured, Off logged confirmed native closure,
  and Quit left zero live workers. Python socket audit observed no runtime
  attempts; 385 native endpoint samples observed none. Physical networking
  was not disabled, and polling cannot prove absence between samples.
- The real smoke's UI screenshot is [stage5-bar.png](evidence/stage5-bar.png).
  The probe records a maximum UI tick gap of about 943 ms during initialization.
  Work is off the UI thread, but Python/native import work can still delay
  event delivery during model loading. Continuous UI smoothness is not claimed.
- Implementation/test Ruff checks and diff whitespace checks pass. The parser,
  gate and both contract-test files were not edited; gate and both contracts
  remain byte-identical to the seed.

An earlier native focus run printed Windows diagnostic `0x8001010d` during Qt
event processing, continued, passed all assertions and exited zero. No
accessibility setting or pytest fault handler was disabled. Its exact native
cause is not established. The latest full suite did not print that diagnostic;
the real app smoke also exited cleanly.

One full-suite rerun failed in the existing
`test_long_text_paste_restores_clipboard`: its read-only fingerprint reported
`Clipboard busy during read-only fingerprint`. The original output is retained
in [stage5-pytest-clipboard-busy.txt](evidence/stage5-pytest-clipboard-busy.txt).
The next unchanged run passed that clipboard check but failed in `Context.save`
after the owned Calculator close: atomic replacement of the test's ownership
record returned WinError 5. That output is retained in
[stage5-pytest-ownership-denied.txt](evidence/stage5-pytest-ownership-denied.txt)
(1 failed, 1,272 passed, one skip in 40.19 seconds). Work stopped at that gate
without a commit. After the user's hands-on confirmation, the latest unchanged
rerun passed both checks and the whole suite. The tests, state-save and
clipboard implementations, expectations, deadlines and delays were not
changed to pass. Neither failure is erased or relabelled as a pass. The
blocking owners remain unknown; this does not eliminate future contention.

Initial probe reports are retained as
[initial](evidence/stage5-app-smoke-initial.json) and
[diagnosed harness error](evidence/stage5-app-smoke-harness-error.json). Those
failures were caused by the probe wrapping DecisionLog without preserving its
`.path` metadata. The corrected probe preserves it. They are not relabelled
as passing app runs. The final UI-only dictation/status changes are covered by
the latest full suite's passing UI tests; the physical capture lifecycle was
unchanged afterwards.

## User-confirmed

The user's 2026-10-12 "everything works" response confirms the four requested
manual checks. These are user observations, not independently captured by the
automated suite:

- Reaching for maximized-browser tabs respects the dwell before expansion.
- Switching Off removes Voicebar's Windows microphone-in-use indicator.
- Monitor unplug/replug and laptop sleep/wake restore the bar's placement
  and listening state.
- A complete session works using only voice and the bar.

## Not verified

- The exact monitor scale factors used in the user's checks, an independent
  recording of hotplug/resume recovery, and both Off and Listening recovery
  cases separately. Real monitor movement passed; mixed-DPI placement is
  covered with logical screen fixtures but lacks explicit physical coverage.
- Presentation over actual exclusive or borderless full-screen applications.
- Personal open-mode pins/false-trigger rate, final live latency targets,
  every optional tray/hotkey/dictation scenario, and extended laptop stability.
- Real sign-in launch from the user's Startup folder.

The repeatable manual sequence, expected outcomes and default shortcuts remain
in [DESKTOP.md](DESKTOP.md#try-by-hand). Stage 6 was not started.
