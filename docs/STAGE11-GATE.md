# Stage 11: command repairs verified; performance acceptance blocked

2026-10-28. The native correctness gate passes, but the requested fast,
simultaneously available speech/text/vision experience is **not accepted**.
The tested command fixes are saved separately from that unresolved performance
claim. Voicebar was not relaunched as a completed release.

## Verified

The complete Windows suite, with unchanged contract expectations and deadlines:

```text
2627 passed, 1 skipped in 71.06s (0:01:11)
```

[Complete output](evidence/stage11-gate-native-isolated.txt). The existing skip
is movement to a second monitor when only one is connected. Ruff passes for
all changed/new Stage 11 Python files; `git diff --check` passes.

- Leading `um`, `uh` and `ah` work as command framing, including before the
  wake name. Literal typing/search payloads retain their text.
- Calculator arithmetic and clear use the active application's observed
  controls. In the real owned-window replay, `type two plus three` produced 5,
  `ah clear calculator` produced 0, and `uh do six plus seven` produced 13.
- Both plain and role-qualified clicks enter foreground-bound grounding.
  `click on the button Seven` produced 7. A real screenshot plus observed
  controls resolved `click the number seven`, producing 77 after the preceding
  click. No screenshots were saved.
- An ambiguous `click on memory` asked for a label and role without clicking.
  `click the tab Memory` then selected the observed Memory tab. `click this`
  refused. Regression tests also reject role-qualified/punctuated unbound
  references, including `click the button this` and `click that?`.
- The native replay closed its own Calculator. Other applications were not
  manipulated. [Native evidence](evidence/stage11-desktop-role-grounded-final.json).
- `open Google tab` no longer accidentally launches Property Tab Builder.
  Site names come from the catalog; this is not a Google-specific action.
- CUDA cache release preserved all 40 baseline transcripts/plans and recovered
  approximately 2.13 GiB of unused allocation. It is a memory-sharing fix.
- Lifecycle tests verify ordered preparation, one preload worker with a
  replaceable pending job, stale-result rejection, bounded retired handles,
  and preservation of busy/timeout/error feedback.
- Atticus source was reviewed at a pinned commit, not executed. Findings and
  adaptation boundaries are in [the source review](STAGE11-ATTICUS-ANALYSIS.md).

Immutable SHA-256 hashes remain unchanged:

| File | SHA-256 |
| --- | --- |
| tests/test_parser_contract.py | DED62BA9134934237BBB2C2E5FE2EE064503AAA7285AC61A9B1A7E43DCD943EB |
| tests/test_gate_contract.py | 60AB126F07314276D835E7308449A34081B4A26D3B579F0F9EA93FF65FFBA8CE |
| voicebar/gate.py | C8B80E3D2269AA4E0141DB97540A9C838E251D12BA1FBBB9400D561623206CA5 |

## Failed or not verified

- The under-400 ms speech-to-action target has not been met. The accepted
  speech model's fresh isolated final-decode median/p95 were 1,143/1,784 ms,
  excluding endpoint waiting and native action execution.
- Reliable simultaneous operation of the three configured models is not
  established. The failed combined probe reached zero free GPU memory and a
  47-second recognition. The new lifecycle ordering is unit-tested, but does
  not reduce the sum of resident weights. No passed combined-model release
  result is claimed.
- Lower-memory alternatives failed unchanged acceptance requirements:
  smaller speech 30/40, int8 speech 34/40, NF4 speech 34/40; CPU text selection
  27/60, partial GPU text selection 46/60. Both quantized speech candidates
  introduced new incorrect typing payloads and were slower. Partial vision
  exceeded its five-second real-image deadline. None was promoted.
- In the successful isolated desktop replay, screen resolution took 3,123 ms
  for the role-qualified Memory tab and 1,814 ms for semantic Seven. These
  include observation and selection, exclude speech recognition, and are not
  evidence of fast whole-app operation. Synthetic vision warmup took 9,326 ms.
- Physical microphone commands, sustained whole-app use, mixed-DPI/multiple
  monitors, arbitrary unlabeled controls, and general Canvas/Fusion workflows
  have not been verified. The current closed-action design is not a general
  computer-use agent.

The remaining blocker is the current model/memory design, not missing permission
to proceed. A subsequent change must establish a measured model scheduling or
recognition approach that preserves accuracy and the existing action deadlines.
See [latency investigation and failed experiments](STAGE11-LATENCY.md).

## Test failures retained and repaired

Earlier full runs encountered a stale generated command reference and Windows
Access Denied while atomically replacing temporary files under workspace
`.cache`. The reference was regenerated. A controlled comparison retained the
same writers: workspace replacements failed, while normal Windows Temp passed
4,000/4,000 writes. The responsible locking process was not identified.

The first full run in Windows Temp still had one test ownership journal under
workspace `.cache`, plus a Windows foreground-activation refusal: 2 failed,
2,625 passed, 1 skipped. The native fixture now places that journal in pytest's
retained temporary directory before opening any window. The desktop probe does
the same in its own unique Windows Temp directory. Atomic persistence, ownership
guards, assertions, timeout bounds and cleanup are unchanged. No retries or
sleep-based workaround was added. The complete rerun above passed.

An existing startup test also raced its harness's automatic start. An explicit
`autostart=False` at that test's intended manual-start boundary preserves all
its original ordering assertions. The harness defaults remain unchanged.

The unrelated README edit and historical untracked Stage 3/7/8 artifacts remain
untouched. Experimental quantization dependencies stay isolated under `.cache`;
application dependencies, lockfile and default speech model are unchanged.
