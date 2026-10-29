# Stage 4 implementation — complete

The original Stage 4 implementation gate passed: the full suite, immutable
seed contracts, actual WAV-to-text-to-gate checks in both modes, named and
unnamed command checks, and the physical Windows lock/unlock stream log. The
latest runtime revision has 1,227 passing tests and the existing physical
second-monitor skip. The user asked to close the stage if its implementation
was done; this closes implementation, with human validation listed separately.

The [third human name-mode soak](evidence/stage4-human-soak-20261011T051507Z.json)
captured 600.032 seconds, finished at a natural endpoint, drained recognition
and closed cleanly. All 113 processed decisions were ignored, with zero
would-run entries. The ending fix therefore worked on the real microphone.
One detected phrase at 3:35.936–3:57.408 exceeded the unchanged twenty-second
cap and was discarded whole. This intentional safety refusal happened during
the run; the movie did not cut the soak short. The original report remains
`complete: false` because that 21.472-second interval was not transcribed.
The recorded interval includes pre-roll and endpoint silence; its acoustic
contents cannot be reconstructed from this log.

Verified: full requested capture duration, successful natural ending, 113
ignored decisions, no plans or failures, clean microphone/backend closure,
and unchanged runtime/test/config/model sources since the passing gate.
Not verified: full transcript coverage of this human run, an open-mode soak
with personal pins, the user's final mode choice, five-minute real use, or
new physical lock and live-latency measurements. Earlier physical lock evidence
remains historical. The diagnostic requirement for complete transcript
coverage is kept in reports; it was not an extra implementation-gate item in
the original stage prompt. No report, cap, test or threshold was changed to
close the implementation stage, and no further name-mode retry is required
for that closure.

## Endpoint repair verification

The second human name-mode soak reached its ten-minute timer and stopped
cleanly: 110 ignored decisions, zero plans, no failures, no live workers, and
confirmed microphone/backend closure. Its report was incomplete solely because
one final phrase was still active when the timer revoked capture. The
[original report and independent measurements](evidence/stage4-human-soak-20261011T043240Z.json)
are preserved without changing `complete: false`. This supports zero triggers
among processed speech; the trailing phrase was not transcribed. Stream-open
to stream-close time was 599.37 seconds, with initial startup outside that
interval. The earlier runaway queue did not recur in this run: maximum STT was
4.29 seconds and maximum queue delay 2.95 seconds.

Timed dry-run soaks now finish at a natural VAD endpoint at or after the
requested minimum, seal that sample interval, close capture, and process all
completed segments inside it. Later callback samples are counted separately
outside the interval. Measurement starts with the first valid captured sample,
so model and microphone startup cannot count toward a passing duration.
No partial phrase is flushed, padded or dispatched. A
single twenty-second ending budget bounds endpoint capture and recognition;
the queue bounds and three-second cleanup joins remain unchanged. Cancellation,
lock, Off and failure still revoke immediately. Missing endpoints, audio drops
or unfinished processing produce an incomplete report.

The new ending behavior and its source-bound checks are recorded in
[endpoint validation](evidence/stage4-soak-endpoint-validation.json).
An actual local Qwen/CUDA
[paced saved-PCM probe](evidence/stage4-soak-endpoint-probe-20261011T050202Z-13cb3181.json)
passed: a 1.2-second timer expired mid-sentence, capture reached its natural
endpoint at 4.736 seconds, the complete final transcript matched the earlier
WAV run, and a later named command stayed outside measurement. The endpoint
was inside the original talk WAV, before the fixed inter-utterance gap. The
report was complete with one ignored decision, no drops, no pending work and
clean shutdown. This used no real microphone and no executor.
The third human run above subsequently verified the ending change on the
microphone, while retaining incomplete transcript coverage. Open-mode soak,
the mode choice and five-minute real use remain pending.

Current full regression gate:

```text
uv run pytest -q
1227 passed, 1 skipped in 21.06s

uv run ruff check voicebar tests scripts docs/evidence/experiment-source/stage4-soak-endpoint-probe.py --exclude tests/test_gate_contract.py
All checks passed!
```

The skip remains the existing physical second-monitor case. Gate/parser
contracts remain included in pytest and byte-identical to the seed copies.

The current actual local Qwen/CUDA
[WAV gate](evidence/stage4-wav-20261011T050239Z-730ccb56/report.json) also passed:
138 WAVs produced 140 final segments, with zero conversation plans in both
modes and all named/unnamed command checks passing. Every final transcript
matches the preceding run. Its recorded fingerprint matches the current
source and corpus; this replay opened no microphone and ran no actions.

## First soak repair (historical)

The first human name-mode soak stopped on its own after 228.7 seconds, with
46 ignored utterances and zero plans. The microphone closed 253 ms after
authorization was revoked. The final cleanup message hid the original failure,
so this is an incomplete run, not a zero-trigger ten-minute pass. The
[recovered report](evidence/stage4-human-soak-20261011T035801Z-incomplete.json)
preserves that distinction and binds the original local log hash.

Completed speech was unnecessarily decoded as a stream of provisional
prefixes, even though the listener uses only finals. The corrected explicit
completed-segment path preserves every VAD frame, final boundary, context and
token-limit refusal while omitting unused provisional model calls. Streaming
PTT and benchmark partials retain their previous behavior. The corrected
[WAV gate](evidence/stage4-wav-20261011T040923Z-581c6f54/report.json) passed:
all 140 actual final transcripts are identical to the prior run, with zero
conversation plans in both modes and unchanged named/unnamed outcomes.
Long-sentence decode median was 1,674 ms versus the earlier 6,831 ms in this
unpaced saved-audio set. These runs do not establish live latency or causally
isolate hardware/scheduling changes.

Failure logs now retain the original error and cleanup diagnostics. Soak saves
an atomic report after success, failure or interruption, including processed
triggers, elapsed time, decision counts, unprocessed speech and cleanup status.
An incomplete empty report is never a pass. Only requested-duration completion
with confirmed cleanup and complete processing coverage sets `complete: true`.
At a timed cutoff, unfinished speech or recognition backlog is conservatively
reported as incomplete; all processed would-run entries are still printed.
Queue bounds and cleanup deadlines were not increased. No new microphone run
has verified sustained listening after this correction.

Current follow-up regression gate:

```text
uv run pytest -q
1194 passed, 1 skipped in 28.61s

uv run ruff check voicebar tests scripts --exclude tests/test_gate_contract.py
All checks passed!
```

The skip is the existing physical second-monitor case. The new checks cover
completed-segment decoding, preserved streaming partials, bounded backlog
failure, shutdown races, original-error preservation, atomic partial reports,
and capture coverage. A timed run with no validated microphone frames or a
Locked/Off interval is incomplete. The immutable contract remains included in
pytest and byte-identical to its seed.

The original implementation gate below is historical. Current follow-up checks
and source hashes are recorded in
[soak repair validation](evidence/stage4-soak-repair-validation.json).

The requested implementation and automated/hardware gate passed on 2026-10-10
local time (2026-10-11 UTC). The earlier blocked checkpoint is preserved in
[STAGE4-INITIAL-GATE.md](STAGE4-INITIAL-GATE.md). The user resumed the stage and
explicitly approved replacing the conflicting native browser test with
simulated browser coverage plus real owned-Notepad refusal checks. Native
browser behavior remains unverified. No parser or gate expectation changed.

## Verified

Final full suite:

```text
uv run pytest -q
1165 passed, 1 skipped in 25.44s
```

The existing physical second-monitor test is the one skip: this laptop has one
monitor. Native mutation tests use their own Notepad/Calculator windows. Browser
coverage preserves the previous safety assertions through simulated dispatch,
adds nine real Notepad tab/navigation refusals before input, and checks installed
browser association metadata read-only. No browser was launched for the final
gate.

Implementation lint:

```text
uv run ruff check voicebar tests scripts --exclude tests/test_gate_contract.py
All checks passed!
```

Unfiltered Ruff reports only I001 in the unchanged seed gate contract. That
file was left byte-identical; the pytest gate did not exclude it.

The final byte comparisons match the seed copies:

| File | SHA-256 |
| --- | --- |
| `voicebar/gate.py` | `c8b80e3d2269aa4e0141db97540a9c838e251d12ba1fbbb9400d561623206ca5` |
| `tests/test_gate_contract.py` | `60ab126f07314276d835e7308449a34081b4a26d3b579f0f9ea93ff65ffba8ce` |
| `tests/test_parser_contract.py` | `ded62ba9134934237bbb2c2e5fe2ee064503aaa7285ac61a9b1a7e43dcd943eb` |

`.gitattributes` preserves the gate files' bytes during checkout.

The WAV gate synthesized the complete 47-entry contract conversation list plus
ten longer ordinary sentences and six named/unnamed command pairs, at two
speaking rates: 138 WAVs, 140 detected speech segments. The shared live
segmenter fed accepted local Qwen/CUDA recognition and continuous gate state in
both modes. Every source had a nonempty transcription. Results:

```text
name mode: conversation plans 0; named commands 12/12; unnamed commands ignored 12/12
open mode: conversation plans 0; named commands 12/12
Python runtime socket attempts: 0
```

Open-mode test pins were Notepad and Calculator; configured pins remain empty.
Command cases include recorded silence to expire follow-up; conversation cases
retain continuous gate state. The final evidence check revalidated all WAV
hashes and the current code/config fingerprint. Full transcripts and decisions:
[WAV report](evidence/stage4-wav-20261011T033309Z-77b1c954/report.json).

Dedicated personal microphone recordings produced:

```text
Computer. Open Notepad.
Computer. Open calculator.
Computer maximize Notepad.
```

These actual texts produce the expected plans in both modes. A fourth
0.608-second clip transcribed empty and was ignored. All synthetic and personal
wake prefixes are exactly `computer` after normal normalization. The explicit
`names: [computer]` configuration needs no extra variant; matching remains exact.
The earlier incidental recordings were retained as separate evidence, without
assigning them expected commands.

The ordinary `.venv` now contains the accepted pinned Qwen/CUDA dependencies.
The dedicated recordings were transcribed again with:

```powershell
uv run --offline python scripts/transcribe_stage4_wake.py --directory fixtures/mine/stage4-wake-20261011T034311Z-aa1ceeab
```

All four transcripts matched the accepted `.venv-stt` runtime. Model parameters
and inputs used `cuda:0`, with BF16 weights. No launcher or second Python
application process is needed. Primary-runtime evidence is
[here](evidence/stage4-personal-wake-20261011T034853Z-430b0de9/run.json).

The user physically locked and unlocked Windows during a record-only probe
using the real listener capture/controller and default microphone. The probe
ran no ASR or actions, retained no audio in lock-only mode, and confirmed valid
frames after reopening. Actual JSON log lines:

```jsonl
{"time":"2026-10-11T03:40:14.511392+00:00","event":"state","mode":"name","state":"locked","reason":"stream_closed","stream_open":false}
{"time":"2026-10-11T03:40:14.770034+00:00","event":"state","mode":"name","state":"listening","reason":"control","previous":"locked","generation":2}
{"time":"2026-10-11T03:40:15.410140+00:00","event":"state","mode":"name","state":"listening","reason":"stream_opened","stream_open":true,"device":{"name":"Microphone Array (Intel® Smart ","index":1,"selection":"system-default-input","sample_rate":16000,"hostapi":"MME"}}
{"time":"2026-10-11T03:40:15.695796+00:00","event":"state","mode":"name","state":"off","reason":"stream_closed","stream_open":false}
```

The [hardware report](evidence/stage4-hardware-20261011T033954Z-f4685fb7.json)
retains all transitions. Capture and workers were closed at the end.

The full suite also covers execution-completion follow-up timing, failed-action
handling, exact wake matching, pinned-app/open-mode restrictions, asleep versus
Off, lock/unlock restoration, queued-audio revocation, 60-second VAD inactivity
dictation exit, whole-segment chime rejection, inert unresolved routing,
bounded queues, missing-frame/worker failures, and dry-run soak reports. State
and decision events are exposed for Stage 5. Logs include time, transcript,
mode, state, reason, plan and per-step timings.

The machine-readable [final validation](evidence/stage4-final-validation.json)
binds these results to source/config hashes and records runtime packages.

## Not verified

- Human ten-minute soaks in either mode and five-minute real use remain pending.
- Actual microphone lifecycle and actual WAV recognition were checked
  separately; no spoken command executed against a real app in this stage.
- No new end-to-action latency or crash-stability claim is made. The accepted
  Stage 3 Qwen result remains 35/40 with speech-end p95 2,538 ms; the original
  latency goals remain unmet. Short successful checks do not resolve the
  previously reported laptop crashes.
- Hardware chime audibility and acoustic rejection were not measured; overlap
  handling is covered by controlled tests.
- Python socket refusal was active during recognition, but native sockets were
  not monitored and physical networking was not disabled for these checks.
- Positive native browser behavior and physical second-monitor movement remain
  unverified.

## Human handoff — stop here

`stt.backend: qwen`, `stt.device: cuda`, `mode: name`, `names: [computer]` and an
eight-second follow-up window are configured. Run:

```powershell
uv run voicebar soak --minutes 10
```

Play a podcast/video near the laptop in name mode. The would-run utterance list
should be empty. Then select 10–20 real installed app names for
`config/pinned.yaml`, set `mode: open` in `config/settings.yaml`, and run the same
command while talking to someone. Each reported utterance is a false trigger.
Soak executes no actions, prints the utterance list, and saves
`logs/listen-*.report.json`.

Paste both reports back before any false-trigger changes are made. Decide
whether to keep name mode or accept open mode's measured rate, then use
`uv run voicebar listen` for five minutes. Stage 5 has not started.
