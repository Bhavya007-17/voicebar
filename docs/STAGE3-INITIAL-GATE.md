<!-- completed-recheck-20261010 -->
**Update: the complete repaired-cue 40-command pass has been evaluated.** The user confirmed usual background noise. Best exact plans: 28/40 (70%). The accuracy gate remains failed. See [the latest recheck](STAGE3-RECHECK.md); no further recording pass is requested now. Earlier results below remain unchanged.

# Stage 3 gate — stopped on accuracy

Both complete personal recording passes were evaluated without executing any
plans. **No candidate passes the 90% exact-plan gate; no speech engine is selected.**
`config/settings.yaml` retains `stt.backend: null`, so live voice execution is
unavailable. Stage 4 always-on listening and Stage 5's bar remain queued.

## Personal recordings: results before new aliases

| Candidate | Exact plans | Quiet | Noise | EOS p50 ms | EOS p95 ms | Null plans |
| --- | --- | --- | --- | --- | --- | --- |
| Moonshine Small Streaming / CPU | 8/80 (10.00%) | 3/40 | 5/40 | 3019.0 | 11412.1 | 66 |
| Parakeet int8 / CPU | 9/80 (11.25%) | 3/40 | 6/40 | 311.6 | 907.3 | 62 |
| Parakeet float32 / RTX 5070 | 13/80 (16.25%) | 3/40 | 10/40 | 218.7 | 509.5 | 58 |

The same 80 accepted, checksummed WAVs were used for every candidate: 40 quiet and
40 with background noise. One abandoned zero-recording session was excluded.
Expected plans, parser thresholds, and aliases were unchanged. CPU Parakeet uses
int8 weights; GPU Parakeet uses float32 weights from the same repository revision,
so this compares the usable configurations rather than precision-matched devices.

EOS is estimated by the common Silero CPU reference, with 32 ms frame resolution;
these are not manually annotated speech endpoints. Timings include streaming
backlog and endpointing. The same two clips per candidate have neither a final
event nor a reference speech-end estimate. They remain accuracy failures and
have no timing sample; percentiles use 78 cases. Parakeet CPU also emits empty
final text on five clips with detected speech; those measured failures are included.
First inference is included; model load is measured separately. Replay is paced
but reaches EOF and flushes, as PTT would. This does not validate always-on use.

The fastest candidate's **EOS p95 is 509.5 ms**, above the 300 ms goal. Its
release-to-final p95 is 285.0 ms; releasing a key is a different endpoint and does
not turn the EOS result into a pass. VAD segmentation, remaining model work and
streaming backlog contribute; their individual causal shares are not isolated.
Moonshine showed substantial CPU backlog on this laptop. CPU and GPU runs were
sequential, under normal interactive system load, rather than an isolated lab.

## Why the gate stopped

Transcripts frequently omit or alter initial command words. For example,
“open Visual Studio Code” became “Visual Studio Code”; “switch to Google Chrome”
became “To Google Chrome”. Name substitutions, missing final text, and changed
dictation/search payloads also occur. Some incorrect transcripts parse into a
different valid action, so null-plan counts alone understate the failures.

The old recorder printed its cue before Windows delivered capture buffers. The
repair waits until the first nonempty frame has been stored, then prints and
flushes **Ready — say the command**. Startup and cleanup have bounded deadlines;
unreleased native capture prevents another recording in that process. This fixes
a confirmed software race. It does **not** prove that every recognition failure
was clipping, or that the microphone's hardware processing is settled at that
first callback. The old recordings and results are preserved unchanged.

See `Stage3-Failures.md` for every failed expectation and a conservative grouping.
App-target rejection counts (33 / 31 / 26) mean the expected plan included an app
and the actual plan was null; they are **not** counts of proven app-name-only
mishearings. No proposed alias has been added, and aliases cannot restore missing
command verbs or correct arbitrary payload changes.

| Candidate | Initial command word absent | Unresolved target | Changed payload | Different valid plan | Empty text | Other |
| --- | --- | --- | --- | --- | --- | --- |
| Moonshine Small Streaming / CPU | 62 | 1 | 1 | 5 | 2 | 1 |
| Parakeet int8 / CPU | 54 | 0 | 1 | 8 | 7 | 1 |
| Parakeet float32 / RTX 5070 | 55 | 0 | 3 | 6 | 2 | 1 |

These categories are exclusive, based on stored transcripts. Wrong valid plans
and changed payloads take precedence over initial-word mismatches. The first
column of failures diagnoses a requested command word absent from rejected text,
not its physical cause. Examples of wrong valid actions include “hit escape”
recognized as “Skip” (media next), and “reopen closed tab” recognized as “Close tab”.

Deferred alias candidates, shown before any addition:

| Heard phrase | Intended app | Decision |
| --- | --- | --- |
| not bad | Notepad | Three transcripts from two clips, all also missing the verb. An in-memory overlay improves zero baseline scores. Confirm on a new clean clip before requesting approval. |
| auto does fusion | Autodesk Fusion | One clip only; confirm on a new clip before requesting approval. |
| cloud | Claude | Already configured; no addition needed. |
| clock | Claude | Do not map: Clock is a different installed app. |
| scloud, Gloria | Claude | Uncertain substitutions; no alias proposed. |
| next cell | Excel | Ambiguous ordinary navigation phrase; no alias proposed. |

The failure file also retains site-name substitutions. No aliases are being added
from this failed baseline, and no approval is needed to leave them unchanged.

## Offline checks and local models

| Candidate | Offline WAVs | Detected endpoints | Runtime socket attempts | Native samples | Largest sample gap ms |
| --- | --- | --- | --- | --- | --- |
| Moonshine Small Streaming / CPU | 288/288 | 0 | 0 | 22911 | 730.9 |
| Parakeet int8 / CPU | 288/288 | 0 | 0 | 2958 | 175.8 |
| Parakeet float32 / RTX 5070 | 288/288 | 0 | 0 | 2867 | 252.3 |

Each proof used a fresh process, verified model hashes, library offline switches,
Python socket/DNS audit guards and native Windows IPv4/IPv6 TCP/UDP sampling.
No runtime socket attempts or native endpoints were detected. The check also
tracked the actual worker and descendants under a scoped job. These are observed
offline runs: adapters/firewall were unchanged, native code can bypass Python
hooks, and connections wholly between samples could escape polling. The gaps
above are retained rather than described as continuous network isolation.

Repeated `scripts/setup.py --cuda` reported:
`All local model files match their SHA-256 manifest; nothing downloaded.`
All models live under `models/`. CPU and GPU ORT packages use separate virtual
environments. The CUDA smoke proved 3,026 encoder and 460 decoder CUDA compute
node executions, rather than merely listing an available provider. Silero,
preprocessing and some shape operations remain on CPU. `docs/CUDA.md` and
`requirements-cuda.txt` document reproduction.

## Synthetic recordings

Windows PowerShell `System.Speech`, Microsoft David Desktop, rates 0 and 2,
generated all 288 WAV cases from the unchanged parser contract.

| Candidate | Exact synthetic plans | Accuracy |
| --- | --- | --- |
| Moonshine Small Streaming / CPU | 257/288 | 89.24% |
| Parakeet int8 / CPU | 255/288 | 88.54% |
| Parakeet float32 / RTX 5070 | 258/288 | 89.58% |

These scores reuse the checked offline-proof transcripts and include the
contract's rejected inputs. They are not personal-voice selection results.
Unpaced decode duration is not EOS latency; paced synthetic latency remains
unverified after stopping at the failed personal accuracy gate.

## Verified and still unverified

- Non-window tests and Ruff: see the final validation line appended below.
- Parser contract is byte-identical to the seed: SHA-256
  `ded62ba9134934237bbb2c2e5fe2ee064503aaa7285ac61a9b1a7e43dcd943eb`.
  All seven seed files still match the supplied ZIP; `CLAUDE.md` and `AGENTS.md`
  match each other. Generated `docs/COMMANDS.md` matches the live grammar.
- Ctrl+Alt+F8 registered and unregistered successfully without opening the mic.
  It uses `RegisterHotKey`, not a low-level keyboard hook, and is configurable.
  This checks the actual registration collision at that time; it does not prove
  every future app shortcut will remain compatible.
- PTT implementation and fake-device tests are present. The CLI requires an
  explicit candidate for a dry-run diagnostic while no engine is selected.
  Partials never execute; capture/recognition failures discard the command.
  Native capture calls run off the controller, with five-second startup/closure
  deadlines and continued release/lock checks. Unconfirmed closure is reported
  honestly and prevents another capture in that process, requiring process exit.
- The earlier clipboard restoration bug was repaired and targeted owned-window
  checks passed (38 passed, 1 physical-second-monitor skip). Later real-window
  tests were paused following the user's concern about the Unicode test string.
  The complete desktop suite has not been rerun after the final speech changes;
  this report does not claim the full Stage 3 pytest gate passed.
- Real PTT “open notepad / type hello world / close it”, Windows microphone-in-use
  indicator, new recorder readiness with this microphone, and physical second
  monitor behavior remain unverified. No new live microphone or window test was
  started by the agent during these recording benchmarks.

## Next human check

Record **five commands only** using the repaired cue before repeating a full set:

```powershell
cd C:\dev\voicebar
uv run voicebar record-set --session check
```

For each of the first five prompts: Enter, wait for **Ready — say the command**,
speak the entire command, Enter to stop, Enter to keep. At prompt 6/40 type `q`
and press Enter. Tell Codex when the five-command check is done. No upload is
needed and nothing executes. We will inspect those recordings as a diagnostic,
without treating five clips as a passed 80-command accuracy gate.

Only after that check succeeds should a replacement quiet/noise set be requested,
clearly marked as recorded after the readiness repair. Old baseline results stay
in the report. No threshold tuning or automatic alias insertion is authorized by
these results. Stage 3 has no passed-gate commit.

## Final source validation

```text
uv run --offline --frozen pytest --ignore=tests/test_windows_integration.py -q
800 passed in 10.84s

uv run --offline --frozen ruff check voicebar tests scripts
All checks passed!

uv run --offline --frozen python scripts/generate_commands.py --check
docs/COMMANDS.md matches the live parser grammar

git diff --check
exit 0
```

This selects tests that do not manipulate real windows; it is not a replacement
for the complete desktop gate. Native microphone, hotkey and application actions
were faked in PTT tests. The accuracy gate remains failed. A source checkpoint
ZIP is provided for review, excluding models, runtimes and microphone WAVs.
