# Stage 7 verification record — updated 2026-10-22

**The physical microphone-to-Claude handoff passed on 2026-10-22.** The user
confirmed success. The actual transcript was `Open Claude and start voice mode.`;
both actions succeeded, the log recorded `post_action_mode: sleep` and
`state_after: asleep`, and a read-only state query found enabled Finish dictation
and Cancel dictation buttons. The app action took 1134.0148 ms; measured
end-of-speech to completed processing was 2456.51 ms. This is one successful
physical trial, not a general reliability or latency claim. See the
[physical voice record](evidence/stage7-live-voice-20261022.json).

Claude must display its chat prompt with Dictate available. A document view
does not expose that control; use `new chat in Claude` first when needed.
Other actions' physical voice checks remain unverified, as listed below.

**The Stage 7 acceptance gate passed on 2026-10-22.** The fresh complete
`uv run pytest` run finished with **2050 passed, 1 skipped in 34.57 s**, exit 0;
output is [stage7-gate-pytest-20261022-final.txt](evidence/stage7-gate-pytest-20261022-final.txt).
The existing one-monitor skip remains; no test was excluded or expectation
changed. `UV_NO_SYNC=1` was set because the running Voicebar launcher prevents
editable-wrapper replacement; dependencies were already installed. This
suppresses environment synchronization only and still runs the complete suite.
Ruff passed on all Stage 7 runtime, test and probe files. Contracts and the
reference gate remain byte-identical to seed.

## Historical verification through 2026-10-12

The first physical trials failed through parsing and action-query timeout issues.
These failures and their corrections are retained below. Native text-transcript
success did not establish physical microphone success at that time.

```text
uv run pytest
2041 passed, 1 skipped in 53.55s
```

Output: [stage7-gate-pytest-final-ready.txt](evidence/stage7-gate-pytest-final-ready.txt).
The existing cross-monitor integration test automatically skipped because
only one monitor is available. No test was excluded or expectation changed.
The handled Windows 0x8001010d focus diagnostic also appeared; pytest completed
with exit code 0. Ruff passes on all Stage 7 runtime, test and probe files.
The two contracts and reference gate remain byte-identical to seed; hashes
are in [stage7-contract-identity.json](evidence/stage7-contract-identity.json).

That complete run used `uv run pytest` with the app stopped, so uv could
check its environment normally. Earlier output is retained in
`stage7-gate-pytest-final.txt` (1980 passed before the live-voice regressions)
and `stage7-gate-pytest-voice-fix.txt` (2012 passed before the final literal
typing regression). No test was excluded in these runs. Runtime deployment
uses default listening Off; startup is recorded in
[stage7-runtime-final.json](evidence/stage7-runtime-final.json).

The first runtime deployment used launch PID 63372. The user's physical check
captured two utterances. `When Claude inserts the dictate mode.` was unresolved.
`Open Claude and start voice dictate mode.` incorrectly parsed as opening Claude,
opening Windows Voice access, then typing `mode`. Voice access was refused by
the elevation guard, and the typing step did not run. See the sanitized
[live failure record](evidence/stage7-live-voice-failure.json). The initial
[runtime snapshot](evidence/stage7-runtime.json) is retained as historical evidence.

The fix reserves unfinished configured action prefixes, so `start voice` cannot
resolve to Voice access during command splitting. Unresolved utterances containing
an app phrase prefix also bypass the generic model selector, which cannot invent
an app action. Claude's definition now explicitly accepts the observed `start voice
dictate mode` wording. Literal typing and explicitly naming `Voice access` retain
their existing behavior. New regression tests cover the failure, foreground
contexts, unsupported tails, fallback refusal and literal payloads. Neither
contract was edited. The old runtime was stopped while its microphone was Off;
a physical retest was still required at that point.

The second physical trial recognized the exact intended wording and opened
Claude's dictation, as the user confirmed. However, the runtime's unchanged
10-second action deadline expired during the verification query, so its log
did **not** record a sleep handoff. A later read found Dictate again. This is
retained as [timeout evidence](evidence/stage7-live-voice-timeout.json), not a
passed gate. Recognition took 8.38 seconds and execution hit 10.05 seconds.

Control lookup now uses a complete ControlView `FindAll` query narrowed by a
canonical role when provided. It reads only candidate identities; action
patterns are fetched for the one selected element. Names and automation IDs
still use Python case/whitespace normalization, and all candidates are read
before selection, so duplicates remain a refusal. Inspect still records the
complete tree. The optimized native Claude chain passed: the action took
2.75 seconds, Finish/Cancel Dictation were queried, and GateSession slept.
Test dictation was canceled. The 10-second runtime action timeout and all
foreground/cancellation guards remain unchanged. A new physical trial was still
needed at that point; the successful 2026-10-22 trial is recorded above.

The first complete gate on the optimized UIA code had 2038 passed, 1 skipped,
1 failure: the existing native Notepad foreground assertion saw HWND 65964
instead of the owned Notepad HWND. A read-only query identified HWND 65964 as
class `ForegroundStaging`, owned by Windows `explorer.exe` PID 13612. Its title
was empty. That failure is retained in `stage7-gate-pytest-uia-final.txt`.
The test, native activation code, foreground guards and limits were left
unchanged for the next complete gate. No user-input cause is claimed.
That unchanged rerun passed 2039 tests. A final post-query foreground/cancellation
recheck and two regressions were then added for waits; the final full run above
passed 2041 tests. The optimized generic click was also rerun on a newly opened
owned Calculator: One changed the display to 1, and a nonexistent control
returned candidates without changing it. That Calculator was closed.

For push-to-talk, hold Ctrl+Alt+F8, wait until the bar shows Hearing, say `open Claude
and start voice mode`, then release and leave Claude in the foreground while
recognition and its large UI Automation tree finish. Microphone startup took
about one second in the recorded trial; speaking before it opens can lose the
start of the utterance. From Off, push-to-talk closes the stream again after
processing; successful `then: sleep` is also recorded in the gate log.

## Action status (updated 2026-10-22)

| App / action | Native status and checked state | Evidence | Physical voice |
|---|---|---|---|
| Claude — New Chat | Works in trials: Ctrl+N reached `New session - Claude` and its prompt edit | [Claude](evidence/stage7-claude-verified.json) | Not verified |
| Claude — focus message box | Works: focused element was `Write your prompt to Claude`, with keyboard focus true | [Claude](evidence/stage7-claude-verified.json) | Not verified |
| Claude — start voice input / voice mode | Physical trial passed: both actions succeeded, enabled Finish/Cancel Dictation queried, and gate asleep. App action 1134.0148 ms; requires the chat prompt | [Physical success](evidence/stage7-live-voice-20261022.json), [historical parse failure](evidence/stage7-live-voice-failure.json), [historical timeout](evidence/stage7-live-voice-timeout.json) | **Passed 2026-10-22** |
| Chrome — switch tab named X | Works in isolated accessible profile: changed Alpha to exact `Voicebar Beta`; duplicate titles refuse | [Chrome](evidence/stage7-chrome-verified.json) | Not verified |
| Chrome — new window | Works: exactly one new window appeared, titled `New Tab - Google Chrome` | [Chrome](evidence/stage7-chrome-verified.json) | Not verified |
| Chrome — new incognito window | Works: one new window titled `New Incognito Tab - Google Chrome`, with incognito page controls | [Chrome](evidence/stage7-chrome-verified.json) | Not verified |
| Chrome — search tabs | Unverified: Ctrl+Shift+A was sent, but the captured tree was identical to the prior tree and showed no search panel. Immediate capture may have missed an asynchronous popup; no success claim | [Chrome](evidence/stage7-chrome-verified.json) | Not verified |
| Word — new blank document | Works from the observed startup screen: Alt,F,N,L with named control waits reached `Document1 - Word`. Ctrl+N had opened templates, so it was not used | [Word](evidence/stage7-word-verified.json) | Not verified |
| Fusion — New Design through MCP | Flaky at startup: a 3 s timeout still created one document later. Final warm trial succeeded, open-document count 1→2 and the new document active. Never retry an unconfirmed write automatically | [Warm success](evidence/stage7-fusion-new-final.json), [timeout state](evidence/stage7-fusion-after-timeout.json) | Not verified |
| Fusion — Fit View through MCP | Works in final trial: script returned fitted=true and a separate read-only query returned finite camera eye/target/extents | [Fit](evidence/stage7-fusion-fit-final.json) | Not verified |
| Fusion — Create Sketch through MCP | Failed: fixed script reported `Fusion could not start Create Sketch`; readback remained `SelectCommand`. The installed API prohibits CommandDefinition.execute within command events, which may explain this MCP script-context failure; command availability versus execute refusal has not been distinguished | [Failure](evidence/stage7-fusion-sketch-diagnostic.json), [readback](evidence/stage7-fusion-command-state.json) | Not verified |
| Foreground — click name | Works in owned Calculator: `click one` changed `Display is 0` to `Display is 1`; unknown name returned candidates and kept the display unchanged. Duplicate-match refusal is covered with fake adapters | [Click](evidence/stage7-click-verified.json) | Not verified |

Each success above is based on an actual state query, not just a sent key or an
executor ok flag. The Chrome trials used a separate profile and local file pages
with renderer accessibility forced on. Normal-profile accessibility and tabs
in other Chrome windows are not established. Claude's existing window was
used only after explicit user authorization; test dictation was canceled
without sending a message. Other native trials used newly opened owned apps.

The earlier native `computer open claude and start voice mode` transcript produced
open_app + app_action, confirmed Claude's Finish Dictation control, and left
GateSession asleep. Its test dictation was canceled. The bar's actual microphone
captured the earlier failed trials detailed above. The separate 2026-10-22
physical record now establishes the successful microphone-to-Claude handoff.

Verified separately: strict YAML rejection, fixed MCP policy and cancellation,
foreground/ownership/identity guards, all-or-nothing plans, exact selection,
ambiguous/missing control candidates reaching feedback, sleep only on success,
CLI inspect and real pointer watch, native states above, historical full pytest
and Ruff, the successful 2026-10-22 physical Claude voice handoff, and the fresh
complete 2026-10-22 pytest gate and Stage 7 Ruff checks.

Not verified separately: other physical voice actions in this table,
general reliability across app updates/locales, normal-profile
Chrome accessibility, cross-window named tab search, and a usable Create Sketch
MCP action. The table deliberately retains failed and unverified entries.

Earlier failed output is retained: `stage7-gate-pytest.txt` (foreground refusal
and the resulting orphan test window) and `stage7-gate-pytest-rerun.txt` (one
ownership-file access failure). Contexts previously shared one per-process
ownership path. Stage 7 now gives each context and atomic temporary write a
unique filename, preserving cleanup provenance. A fake regression test checks
that contexts in the same process cannot erase each other's ownership record.
The exact cause of the Windows temporary-file access denial was not established;
the final run passed after this fix, without retry delays or changed thresholds.

The complete Stage 7 acceptance gate is satisfied with the recorded limitations.

Historical follow-up earlier on 2026-10-22: a physical attempt was recognized correctly but
blocked by Fusion's removed pinned executable, before Claude could activate.
The exact local override now points to the verified installed Fusion360.exe
under deployment `736d2b4c5e6b95e77c11721da222cf21c6a1fc1f`. Context creation and
an unchanged runtime-state write/readback pass. Claude's document view at that attempt
does not expose Dictate; after the authorized New Chat test, its native voice
handoff passed in 1.01 seconds with Finish/Cancel Dictation queried and the gate
asleep. Test dictation was canceled without sending a message. See
[new native result](evidence/stage7-claude-chain-20261022.json) and
[document-view refusal](evidence/stage7-claude-document-view-20261022.json).
83 focused tests passed before the later physical trial; the running bar reads
the repaired setting on the next command. The subsequent physical trial passed
as recorded at the top of this file. The pre-closeout full suite passed with
2041 passed and the existing one-monitor skip in 31.86 seconds; that earlier
output remains in `stage7-gate-pytest-20261022.txt`.

Final closeout review found that successful named app actions did not persist
their app for later CLI or standalone PTT pronouns. Successful configured
actions and exact Chrome tab selections now save that app; failed or incomplete
actions and generic foreground clicks preserve the previous named target.
New fake-UI tests exercise real state writes and both CLI/PTT state readers.
The final complete gate after this correction passed 2050 tests with the existing
one-monitor skip in 34.57 seconds, and Ruff passed on the modified code and test.
The successful GUI physical voice handoff above remains its separate evidence.
