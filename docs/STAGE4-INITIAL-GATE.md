# Stage 4 checkpoint — blocked

The gate and always-on listener have been added, but the Stage 4 gate has not
passed. The user chose to retain the Notepad/Calculator target rule and report
the full suite blocked, rather than authorize the existing browser integration
test as an exception. No passed-gate commit was made. Prior Stage 3 changes in
the working tree were retained.

## Verified

The seed gate and its contract were copied without changes. The initial check:

```text
uv run pytest tests/test_gate_contract.py -q
126 passed in 0.27s
```

Final focused checks, using fake microphones, clocks, recognizers and executors:

```text
uv run pytest tests/test_listening.py tests/test_listening_races.py tests/test_audio.py tests/test_cli_stt.py tests/test_gate_stage4.py tests/test_gate_contract.py -q
192 passed in 0.84s
```

These cover bounded segmentation, timestamps, whole-utterance rejection on
overflow/discontinuity, wake/follow-up behavior, completion-based follow-up
timing, dictation timeout, chime rejection, inert unresolved routing, dry-run
behavior, Off/Locked stream context closure, state restoration, and shutdown
revocation while recognition is blocked. Fake lock transitions are not a claim
about a physically locked Windows session.

Byte identity was checked for all three pairs:

| Actual file and seed counterpart | SHA-256 |
| --- | --- |
| `voicebar/gate.py` | `c8b80e3d2269aa4e0141db97540a9c838e251d12ba1fbbb9400d561623206ca5` |
| `tests/test_gate_contract.py` | `60ab126f07314276d835e7308449a34081b4a26d3b579f0f9ea93ff65ffba8ce` |
| `tests/test_parser_contract.py` | `ded62ba9134934237bbb2c2e5fe2ee064503aaa7285ac61a9b1a7e43dcd943eb` |

Local file-only synthesis produced 138 PCM16 mono 16 kHz WAVs under
`fixtures/gate-stage4/`: the entire 47-entry contract CONVERSATION list, ten
longer ordinary sentences, and six named/unnamed command pairs, each at two
rates. The manifest records configured pins separately from the Notepad and
Calculator pins used for the proposed open-mode check. No audio was played.

The available completed personal recording manifests contain 120 prompts and
zero wake-name prompts. Existing synthetic Stage 3 prompts also contain no
wake-name examples. `names: [computer]` is explicit configuration, with no
invented recognition variants or fuzzy matching.

## Full-suite failure and stopping point

The requested full suite was run once after copying the gate:

```text
uv run pytest -q
FAILED tests/test_windows_integration.py::test_installed_default_browser_identity_and_site_close
voicebar.executor.common.ExecutionError: No eligible foreground window
1 failed, 1111 passed, 1 skipped in 18.81s
```

The failure occurred at `ctx.target().title`, after the test's owned browser
was opened and the mismatched-site close was refused. The test assumes that
the global foreground still belongs to its browser. The recorded browser
identity still existed. No sleep, threshold change, expectation change, skip,
or retry was used to conceal the failure. The existing second-monitor skip was
already in the suite.

Review also identified that this existing test targets a real browser, contrary
to CLAUDE.md rule 7: “Use Notepad and Calculator as targets.” The user explicitly
declined an exception and chose “Keep the Notepad/Calculator rule; report the
full gate blocked.” The browser test remains unchanged by Stage 4. Resolving
that test policy requires a later user instruction; changing it to a mocked
test here would not establish equivalent native coverage.

## Not verified

- No Qwen/CUDA Stage 4 inference was run. The user selected that runtime for
  focused checks, but the full-gate block stopped inference before it began.
- Zero conversation plans and named/unnamed WAV behavior remain unverified.
  Prepared files alone do not pass the WAV gate.
- No personal wake-name audio was recognized, so personal variants are missing
  input rather than a verified empty set.
- No actual microphone, physical session lock/unlock, or real listener execution
  was tested. There are no real session-lock stream-closure log lines to show.
- No end-to-action latency, crash stability, final native network monitoring, or
  human soak/real-use test was established.
- Live recognition stays disabled through `stt.backend: null`; selecting an
  accepted benchmark for diagnostics did not enable execution.

`voicebar listen` without `--ptt` and `voicebar soak --minutes N` now have gated
listener paths. Soak always uses a dry executor, simulates gate modes/follow-ups,
logs locally, and writes a final report of utterances that would run. These paths
remain unverified with real hardware. No soak test is requested at this blocked
checkpoint.

The prepared future WAV validation command, once the stage is resumed, is:

```powershell
.venv-stt\Scripts\python.exe scripts/gate_stage4.py --validate --backend qwen --device cuda
```

It retains per-segment checkpoints and failed-run reports, uses continuous audio
and gate state in each mode, and requires nonempty transcript coverage. Its
Python socket audit does not claim native network isolation or hardware-offline
verification. Qwen's dependencies remain in the separate accepted candidate
runtime, not the ordinary `uv run` environment.
