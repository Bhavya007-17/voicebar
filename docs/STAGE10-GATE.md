# Stage 10: foreground repair and local screen selection

Native gate passed on 2026-10-25: **2503 passed, 1 skipped in 40.83s**.
The existing skip is cross-monitor movement with only one connected monitor.
See [complete output](evidence/stage10-gate-native-final.txt).

The user reported that Calculator opened but follow-up input and screen control
failed. The live log identified duplicate installed Antigravity entries as the
foreground-identification failure. The repair resolves observed identity directly.
The local screen fallback adds a transient foreground image and bounded model
selection among Windows-exposed controls. It does not provide universal visual
clicking or a multi-step planner.

Verified:
- The machine's existing catalog resolves Calculator despite two Antigravity
  registrations (read-only reproduction before approval became unavailable).
- The local vision model downloaded and passed setup's metadata checks; its
  digest is recorded in models/vision-manifest.json.
- Focused tests cover duplicate identities, screen frame expiry/revocation,
  capture bounds, model output validation, stale controls, configuration,
  unchanged command contracts, and runtime integration. See the evidence output.
- Full native gate above passed, including owned Notepad/Calculator execution.
  Ruff passed on all 27 changed/new Python files; git diff --check passed.
- Both `type two plus three` and `add two plus three` identify the real
  Calculator foreground and produce `Display is 5`. Input starts in 164 ms
  and 28 ms in the successful native probe; these are executor timings, not
  end-to-end microphone latency.
- A native screenshot plus 35 observed controls selects Seven (candidate 27),
  clicks it through UI Automation, and reads back `Display is 7`. Warm model
  selection took 772 ms, within the unchanged five-second bound. No screenshot
  was saved. The same probe verifies literal `2+3` typing in Notepad, then
  closes both owned windows. Restored Notepad documents remain untouched;
  a fresh blank document is opened before test typing.
  [Successful native probe](evidence/stage10-owned-probe-grounded.json).
- The official portable Ollama 0.40.11 archive checksum passed. The local
  server reports that version and its logs show the image encoder using CUDA0.
  The system-wide installation is unchanged. See DECISIONS.md for details.
- Independent code review found no actionable issues in frame authorization,
  stale UIA rejection, hold-to-talk lifetime or Calculator readback.
- Relaunched Voicebar, completed speech-weight loading and local model warmup,
  and confirmed its visible tool window by the launched process identity.
  Startup state is Off; no microphone command was injected.
  [Launch evidence](evidence/stage10-app-launched.json).
- Repeated the owned Calculator/Notepad probe with Voicebar and its speech model
  loaded: all checks pass, visual selection 784 ms, synthetic warmup 423 ms.
  [Coexistence probe](evidence/stage10-owned-probe-with-app.json).
- SHA-256 hashes of test_parser_contract.py, test_gate_contract.py and gate.py
  match Stage 9 exactly.

Not verified:
- Physical microphone commands and end-to-end speech latency with this build.
- Mixed-DPI/multiple monitors, arbitrary applications, unlabeled graphics and
  general multi-step Canvas/Fusion workflows.
- Accuracy across a representative screen-command corpus. One native visual
  selection succeeds; a bounded selector can still make a semantic mistake.
- Graphical changes that leave accessible UI content unchanged. Window identity,
  frame age, generation and full UIA signatures are checked; exact pixel equality
  is not used because carets and overlays continuously change.
- Sustained GPU contention and cold-load latency. Timeout safely refuses the
  action; exact-label commands remain available without vision.
- Atticus was reviewed as source, not installed or executed. Its general
  workflow learning remains future work; Calculator result verification is
  implemented. See ATTICUS-REFERENCE.md.

Failures retained and repaired:
- The first full run had six browser-context fixture failures: its fake registry
  lacked the new inverse identity lookup. Added the fake method without changing
  assertions; the complete rerun above passes.
- Focused setup tests initially hit sandbox temporary-directory permissions.
  A workspace basetemp passed all 136 tests, followed by the complete native run.
- Initial vision probes exceeded five seconds because the old runtime forced
  CPU image encoding. The portable version enables GPU offload. Its first
  initialization exceeded the 60-second warmup bound; subsequent synthetic
  warmup took 943 ms.
- A native selection confused a spoken number with a candidate ID. The probe
  rejected it without clicking. The corrected generic prompt matches name/role
  before copying the ID; the successful rerun is linked above. Earlier probe
  JSON files and the failed full-suite output remain in evidence/.
- Approval-service usage limits interrupted work twice. Native verification
  resumed after service availability returned; no check was bypassed.

The unrelated README edit and historical untracked files remain untouched.

Explicit setup: run scripts/setup_vision_runtime.py for the pinned portable
server, scripts/setup.py --serve-resolver in a separate process, and
scripts/setup.py --vision to install the allowlisted model if needed. Normal
Voicebar startup does not start a server or download models. Runtime inference
uses only 127.0.0.1.

The centered translucent pill and two-minute command session from Stage 9 remain.
Toggle listening with Ctrl+Alt+F9 or the bar; say `computer open calculator`,
then `type two plus three`. `click seven` uses an exact label; `click the number
seven` exercises visual selection when a fresh frame is available.
