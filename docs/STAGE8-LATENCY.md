# Stage 8 latency work

The adaptive endpoint interface is implemented; acceptance measurements are
pending. `voicebar.endpoint.PartialPolicy` uses the existing pure parser and a
catalog snapshot. It strips only the wake-name/greeting syntax accepted by the
gate. Unknown/ambiguous prefixes retain the ordinary silence wait. Any typing,
search, dictation, or chain containing free text retains the longer wait.

`SpeechSegmenter` accepts `short_silence_seconds` and `partial_policy`.
`update_partial(text, start=..., speech_end=...)` accepts only a prefix covering
the current utterance's latest VAD-active sample. A prefix from before resumed
speech, a canceled capture, or an earlier utterance cannot shorten anything.
Free-text protection latches until completion/reset. `drain_previews()` emits
one provisional PCM snapshot at each short quiet boundary, for a recognition
worker to consider; these snapshots do not authorize or execute a command.

The settings parser validates two configurable waits, initially 250 ms short
and 800 ms ordinary, plus a conservative `adaptive_endpoint: false` default.
The existing segmenter's 300 ms default is unchanged for callers which do not
opt into these settings. Runtime integration must never treat a final-only
recognizer as a source of already-available live partials.

API choices were checked against this checkout's `voicebar/stt/base.py`,
`qwen.py`, `parakeet.py`, `moonshine.py`, and installed
`.venv/Lib/site-packages/moonshine_voice/transcriber.py`. Installed metadata
reported moonshine-voice 0.1.5 and numpy 2.5.3. No new package or downloaded
model was introduced. The current Qwen backend's streaming method emits
expensive provisional prefix redecodes; the accepted final-only path purposely
omits them. Existing tests specify its internal 300 ms final boundaries.

`scripts/profile_stage8.py` loads the configured local model once, decodes
selected personal recordings twice, separates the first decode from warmed
repeats, and retains a Python cumulative-time profile. It never opens a
microphone or executes a plan. Its standard-library profiling interfaces were
checked in installed Python 3.12.13; model and backend APIs were read locally.

The first profiling attempt was interrupted. Another Voicebar instance still
held the configured GPU model. Read-only GPU telemetry reported 7807 MiB of
8151 MiB occupied and 100% utilization. The first recording decoded as
`Open Google Chrome.` in 196622.023 ms, then the owned profiling session was
stopped with Ctrl+C (exit 1). This is resource-contention evidence, not a normal
latency baseline or a successful profile. No other process was stopped.

After the user closed the competing applications, a host read-only check showed
437/8151 MiB occupied and 4% GPU utilization. The configured Qwen/CUDA profile
then completed. Model initialization was 63773.86 ms; the first command took
4984.23 ms. The same first recording on the warmed repeat took 1848.06 ms.
Across five warmed examples, recognition p50/p95 was 1394.33/2631.55 ms. These
are unpaced complete-WAV decodes, not microphone-to-action latency or a steady
state before/after improvement. Recognition errors are retained in the report.
Evidence: `docs/evidence/stage8-recognition-profile.json`.

In one warmed 1441 ms profile, generation consumed 1396 ms, including 415 ms
prefill and 342 ms greedy-token stop checks. Processor preparation consumed
34 ms and generation-configuration preparation 31 ms. The prompt contained
1662 tokens. No repeated catalog scan appeared in the profile. Installed
Transformers `generation/utils.py` explicitly supports deferred stopping only
for MPS, so its private CUDA guard was not patched. Changing greedy decoding,
context vocabulary or model precision merely for timing was not attempted.

Qwen now exposes an explicit startup `warmup()`. It decodes one second of
locally generated zero PCM through the existing verified generation method,
discards every transcript without involving the gate, and clears temporary
utterance/VAD state while retaining the loaded model. It refuses an active
recording. This moves initialization work ahead of microphone-ready feedback;
it does not change ordinary silent-input VAD behavior or promise better steady
state latency. Fake-backed lifecycle/error tests pass. A subsequent fresh-model
execution of the actual method succeeded: startup warmup took **8101.79 ms**;
the first real recorded command took **1288.84 ms** and its warmed repeat took
**1248.38 ms**, both transcribed `Open Google Chrome.`. The previous fresh-model
run's first command took 4984.23 ms. This one-command comparison moves 8.10 s
into startup and reduced the observed first-command delay by 3.70 s; it does
not measure a steady-state speedup. Model loading remains separate. Evidence:
`docs/evidence/stage8-startup-warmup-profile.json`.

Historical decision logs up to and including
`listen-20261023T023052Z-9029e011.jsonl` contain 348 measured hop rows. Nearest-rank
p50/p95 in milliseconds: end-of-speech wait 320.00/320.00; recognition
993.80/4169.09; gate including parser 0.06/13.73. These historical logs have no
measured execute-start hop, so it is reported unavailable. Action duration is
not used as a substitute. The exact source filenames and counts are retained
in `docs/evidence/stage8-historical-latency.json`. This is historical mixed-use
data, not a controlled matched-recording before/after comparison.

`voicebar.latency` and `scripts/report_stage8_latency.py` summarize only explicit
measured fields, retain malformed-line errors, and show unavailable hops. New
`execute_start_ms` measurements denote the GateSession dispatch boundary and
include preceding gate/foreground/resolver work; they do not prove the native
worker's actual action start. `speech_end_to_execute_start_ms` is separately
recorded by the session owner.

`scripts/benchmark_stage8_endpoints.py` implements unpaced candidate analysis
using actual provisional recognizer decodes. It labels inference costs and
sample-clock endpoint waits separately, retains every new early boundary and
before/after plan, and never provides expected fixture text to recognition or
the endpoint policy. `--all-wavs` includes every WAV under `fixtures/mine`.

An initial harness smoke used five selected personal recordings with the
already-installed CPU Moonshine backend, because the configured GPU remained
occupied. Before/after sample-clock endpoint p50/p95 were 320.00/320.00 and
288.00/740.00 ms. Recognition including all provisional work regressed from
1324.29/2762.95 to 3509.94/4068.46 ms. There were zero new early boundaries in
these five recordings. The baseline itself produced six segments, recognition
was inaccurate on several examples, and this is neither a configured-Qwen
comparison nor an assertion that every spoken command was preserved. Output:
`docs/evidence/stage8-endpoint-moonshine-smoke.json`.

This measurement rejects enabling serialized provisional redecoding by default.
An available partial covering the most recent speech can shorten the endpoint;
running a second expensive decode merely to obtain that partial can make total
latency worse. Completed utterances and changed speech invalidate queued
previews through `preview_is_current`; protected free text does not request
further previews. A native streaming integration still needs measurement.

Verified: 120 focused audio, adaptive, log-summary, complete-segment, backend-base,
and Qwen contract tests passed in 3.31 s using a workspace basetemp. Ruff passed
on all new/changed latency files.
The first test invocation hit the sandbox's temporary-directory permission
failure, and a new test initially used a greeting unsupported by the gate; the
new test was corrected to the gate's supported `hey` syntax. Parser and gate
contract tests were not edited.

The configured Qwen/CUDA raw-WAV comparison subsequently completed all **134/134**
WAVs under `fixtures/mine`, including all 120 selected completed-session takes,
rejected extra takes, and Stage 4 wake/conversation recordings. There were
**zero new early speech boundaries and zero changed final transcripts**.
There were 26 provisional decodes, including 11 accepted complete current
partials; none exercised protected free text. All 11 eligible complete preview
transcripts equaled their corresponding final transcript. Expected-plan
evaluations were 51/120 before and 51/120 after, including the historical
clipped recordings. The comparison does not replace the separately accepted
later 40-recording accuracy result.

| Measured raw-file quantity | Before p50 / p95 (ms) | Candidate p50 / p95 (ms) |
| --- | ---: | ---: |
| Sample-clock endpoint wait | 54.00 / 320.00 | 48.00 / 320.00 |
| Final recognition only | 1545.66 / 3845.99 | 1563.35 / 3741.68 |
| All recognition including previews | 1545.66 / 3845.99 | 1618.75 / 4490.53 |

Many saved WAVs end before the short silence boundary; known EOF flushes cap
their wait. Thus these endpoint percentiles do **not** measure live microphone
silence waits. The run is unpaced candidate analysis with serialized prefix
recognition, not a measurement of live asynchronous partial arrival. The 11
accepted partials exercised shortening; all 134 did not. No gate or executor
was invoked. Actual payload protection remains supported by generated-sample
tests, rather than a protected provisional decode in this run.

All raw rows and costs are retained in
`docs/evidence/stage8-endpoint-candidates.json`; a source-hash-bound analysis is
`docs/evidence/stage8-endpoint-analysis.json`. The result reinforces keeping
serialized prefix decoding disabled by default: p95 total recognition cost
regressed by 644.54 ms. No threshold was lowered, recording excluded from the
all-WAV run, or expectation changed.

Not yet verified: a live native-partial source, real microphone end-to-action
before/after latency, or improved steady-state recognition cost. No successful
Stage 8 latency gate is claimed.
