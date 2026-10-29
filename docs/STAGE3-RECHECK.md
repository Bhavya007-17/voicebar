# Stage 3 recording repair: full 40-command recheck

**All 40 new recordings were evaluated. The 90% exact-plan gate still fails.**
The highest measured agreement is 28/40 (70.0%).
No engine is selected, no aliases were added, and no plans were executed.
More recordings are not requested at this point: the remaining recognition
failures need an engineering decision first. Stages 4 and 5 remain queued.

## New benchmark table

| Candidate | Exact plans | EOS p50 ms | EOS p95 ms | Null plans | Null with expected app |
| --- | --- | --- | --- | --- | --- |
| Moonshine Small Streaming / CPU | 24/40 (60.0%) | 4201.2 | 9219.8 | 13 | 3 |
| Parakeet int8 / CPU | 26/40 (65.0%) | 181.0 | 460.2 | 11 | 3 |
| Parakeet float32 / RTX 5070 | 28/40 (70.0%) | 241.8 | 485.0 | 8 | 1 |

The user confirmed that this set was recorded with usual background noise. Its
saved session label is `check`; a separate human-condition annotation preserves
that information without editing the original recording manifest. The exact
session is `20261010T165301Z-check-0ffa4a3f`. All three runs used the same 40 WAV hashes and expected
plans. None was dropped from scoring. CPU Parakeet uses int8 weights and CUDA
Parakeet uses float32 from the same model revision.

EOS uses the common Silero CPU frame estimate (32 ms resolution), not manually
labeled phonetic endpoints. Streaming replay includes backlog and endpoint wait;
EOF flushes emulate PTT. First inference is included and model load is separate.
No percentile is inferred from the earlier unpaced five-clip diagnostic. The
full reports retain release-to-final separately from end-of-speech latency.

None meets the 300 ms p95 target. Moonshine's shipped CPU engine takes several
seconds to process/finalize these paced replays. Parakeet includes the configured
300 ms silence endpoint and decoding; EOF can flush sooner, so 300 ms is not a
floor for every clip. These runs do not isolate native inference, segmentation
and Windows scheduling costs. CPU int8 versus GPU float32 also means that a
faster GPU result is not assumed.

## Comparison with the original noisy recordings

| Candidate | Earlier noisy pass | Repaired-cue noisy pass |
| --- | --- | --- |
| Moonshine Small Streaming / CPU | 5/40 (12.5%) | 24/40 (60.0%) |
| Parakeet int8 / CPU | 6/40 (15.0%) | 26/40 (65.0%) |
| Parakeet float32 / RTX 5070 | 10/40 (25.0%) | 28/40 (70.0%) |

The earlier 80-recording quiet/noise baseline remains unchanged in
`Stage3-Gate.md` and the original JSON reports. The new cohort is selected
explicitly through `--session`, recorded in the report and fingerprint; exclusion
reasons identify the other sessions. All complete sessions still undergo their
normal provenance checks before selection. This avoids mixing capture protocols
and does not erase the earlier failed results. The comparison supports a large
improvement after the recorder repair, without proving the physical cause of
every individual recognition error.

## Recording integrity and start cue

Verified 40/40 kept WAVs and all expected plans against the current unchanged
contract/catalog/prompt provenance. There were 41 total takes: 40 kept and one
retry. All takes retain first-valid-frame readiness metadata. Every selected
file is checksummed 16 kHz mono PCM16. There were no capture flags, capped takes,
or clipped samples under the existing quality checks.

Windows took a median 1,054.5 ms and p95 1,172 ms to deliver the first stored
capture frame (range 844–1,187 ms). The speaking cue now follows that frame.
Energy checks are not speech labels and cannot prove intact opening words.
The new transcripts retain opening command words more often than the earlier
set; this does not identify the cause of the difference. The full remaining
failures are shown in the companion failure report.

## Remaining errors

See `Stage3-Recheck-Failures.md` and `Stage3-Recheck-Failures.json` for exclusive
diagnostic categories and every failed expected/actual plan. Examples on Parakeet CUDA:

- “close this tab” becomes “Close the stab.”; “next tab” becomes “Next stab.”
- “press control shift t” becomes “Press Ctrl Shift D.”, a different valid shortcut.
- “search youtube for flood fill algorithm” becomes a Google search for different text.
- “open Claude” gains an unexpected “Yeah.” prefix and is rejected all-or-nothing.
- Typed punctuation and search payload changes count as failures exactly as saved;
  no case, spelling, number or punctuation normalization was added to improve scores.

| Observed failure group | Moonshine CPU | Parakeet CPU | Parakeet GPU |
| --- | --- | --- | --- |
| Opening command words changed | 6 | 4 | 3 |
| Target/command phrase unresolved | 5 | 4 | 3 |
| Typed/search payload changed | 1 | 1 | 2 |
| Different valid plan | 2 | 2 | 2 |
| Empty transcript | 0 | 1 | 0 |
| Extra leading words | 2 | 2 | 2 |

These are observed transcript/plan groups, not proven acoustic causes. A target
group can include a misheard browser command phrase, not just an app name.

App aliases do not repair wrong keys, command-word substitutions, unwanted
prefixes or changed typed/search text. Observed alias candidates are `claire`
and `cloy` for Claude, and `octopus fusion` for Autodesk Fusion. Each has only
one recording of evidence; the first two would repair one complete plan each
in different engines on the same `p010` WAV, while the Fusion candidate repairs none. They are listed
for review and deferred; no engine would reach 90% from these changes. `get help`
for GitHub is rejected because Get Help is an installed app and the navigation
words also differ. No alias was added.
No parser/VAD thresholds, grammar expectations or contract files changed.

## Verification and limitations

The original three cold-load offline proofs still cover all 288 synthetic WAVs
with library offline switches, Python audit guards and native IPv4/IPv6 endpoint
sampling: zero detected endpoints/runtime socket attempts. Models and the STT
adapters are unchanged by this recheck. The native polling limitations remain
documented in the original gate report; network adapters were not disabled.

The session-selection change passed 32 fake benchmark tests and Ruff. The prior
800 non-window tests passed before this small selection change. Real-window tests
remain paused after the clipboard-test incident, so this is not a claim that
the complete Stage 3 desktop gate passed. No microphone capture/playback,
hotkey registration, typing or window action was performed in these benchmarks.
The real PTT voice gate and a corrected-cue quiet pass remain unverified. No
passed-gate commit has been created.

## Reproduce this cohort

```powershell
cd C:\dev\voicebar
uv run --offline --frozen python scripts/benchmark_stage3.py --backend parakeet --device cpu --session 20261010T165301Z-check-0ffa4a3f --timing realtime --output docs/evidence/recheck-new.json
```

Use a new output filename; `--resume` requires identical data, session selection,
code and models. Moonshine uses `--backend moonshine`. CUDA uses the isolated
`.venv-gpu` environment described in `docs/CUDA.md` and adds `--device cuda`.
These commands read saved audio and only print/save plans.
