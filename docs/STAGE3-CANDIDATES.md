> Historical catalog-only comparison. Superseded by the user-accepted 35/40 grammar-context v1 closeout in [STAGE3-GATE.md](STAGE3-GATE.md). Heavy testing has stopped; the pending experiments mentioned below will not continue automatically.

# Stage 3 candidate status — interim, 2026-10-10

**Qwen is the most accurate candidate tested so far, at 33/40 exact plans (82.5%). None of these three candidates reaches the unchanged 90% gate.** No backend has been selected, no aliases have been added, and the live push-to-talk gate has not been run. Stage 3 remains in progress. This report covers completed results only; it makes no claim about the command-context experiment currently underway.

## Completed GPU comparison

All three candidates processed the same 40 kept recordings from `fixtures/mine/20261010T165301Z-check-0ffa4a3f`. The user confirmed usual background noise. The original `check` label is preserved, with the noise condition recorded separately. These recordings used the repaired cue: Ready appeared only after the first valid microphone frame was stored. Earlier 80 quiet/noise recordings and their baseline results remain separate historical evidence; they are not pooled into this comparison or overwritten.

| Candidate / execution mode | Exact plans | Estimated speech-end p50 | Estimated speech-end p95 | Release-to-final p95 |
|---|---:|---:|---:|---:|
| Qwen3-ASR 1.7B / CUDA BF16 | 33/40 — 82.5% | 2,044.0 ms | 4,565.5 ms | 4,423.5 ms |
| Whisper large-v3 / CUDA FP16 | 30/40 — 75.0% | 1,218.5 ms | 2,128.5 ms | 1,932.5 ms |
| Whisper large-v3-turbo / CUDA FP16 | 28/40 — 70.0% | 535.5 ms | 1,032.2 ms | 1,032.2 ms |

Each full run has 40 valid results and 40 timing endpoints, with no inference errors, early finals or missing speech-end/final timings. Full action-list equality is the score: typed/search payload capitalization, punctuation and spacing, engine choice, targets and shortcut letters all count. Every recording remains in the denominator. Expected commands are evaluation data, never recognition prompts. Contracts, aliases, parser thresholds and VAD thresholds were unchanged.

Paced replay follows the WAV sample-arrival clock and includes recognition backlog and the first inference. The speech-end reference is a separate common Silero CPU estimate, with at least 32 ms frame quantization and additional VAD uncertainty; it is not a manual acoustic annotation. Release-to-final is a separate measurement. Cold model loading is separate from utterance latency. All p95 values exceed the 300 ms goal; the stage permits reporting the measured delay and limits, while exact-plan accuracy remains below its mandatory 90% gate.

The adapters reuse the existing Silero segmentation: 16 kHz mono, 512-sample frames, onset 0.5, continuation 0.35, 200 ms pre-roll, 300 ms silence endpoint and up to 150 ms trailing padding. They provide provisional prefix redecodes rather than native model streaming, at the existing cadence of at least 0.8 seconds. Expensive prefix decoding can contribute backlog; the current evidence does not isolate how much latency comes from each component. EOF flushing resembles push-to-talk release and does not establish always-on endpoint behavior.

The failure consolidation distinguishes command-word errors, literal payload formatting, wrong search routing, wrong valid shortcuts and isolated app names. No failure is attributed solely to command-grammar case/punctuation matching. Only Turbo's `Close Claw` is repaired completely by the proposed app alias `claw: Claude`: the counterfactual is 29/40 (72.5%), with no installed-name collision found and no regression in these 40 transcripts. It is supported by one unique recording, remains unapproved and unadded, and does not make any candidate pass. See `Stage3-Candidate-Failures.md` and its JSON for every failure and exact action difference.

## CPU checks and interrupted measurements

CPU initialization and inference are reported separately. The repeated probes ran after the user confirmed the laptop was awake, plugged in and had its lid open.

| Awake CPU probe | Model initialization | First WAV outcome |
|---|---:|---|
| Whisper large-v3, INT8 / actual `int8_float32` | 20.5 s | Timed out after 32.802 s from the parent's inference start |
| Qwen3-ASR 1.7B, FP32 / four CPU threads | 43.3 s | Timed out after 32.815 s from the parent's inference start |

The validated first WAV lasts 2.782 seconds. Its controlled inference deadline was audio duration plus the existing 30-second post-arrival recognition allowance: **32.782 seconds**. These are one-WAV deadline probes, not 40-command CPU accuracy results, EOS quantiles, or full endpoint-monitoring proofs. The original 30-second safety allowance was not increased.

Turbo CPU retained only six entries before its owned benchmark processes were stopped after observed delays of roughly 29–47 seconds. That checkpoint is incomplete and is excluded from the full-40 accuracy/latency table. Its partial accuracy and quantiles cannot establish whole-set performance.

Earlier CPU checks and Whisper's unusually long initial GPU cold-load measurement overlapped documented Modern Standby events. Those wall-clock values are preserved but are not used as valid CPU inference/cold-load comparisons. Awake CPU probes replace the inconclusive CPU smoke evidence; the three full GPU utterance runs occurred during awake intervals. The temporary native execution-state request (`ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED`) used for the awake checks was acquired and **released**, as recorded in `stage3-candidates-awake-checks.json`. It did not change a power plan or install a service.

## Offline checks

Each candidate cold-loaded its hashed local assets and transcribed all 40 selected recordings under offline switches, Python socket refusal and native worker/descendant endpoint monitoring.

| CUDA offline proof | WAVs completed | Endpoint samples | Largest sampling gap | Observed runtime attempts / endpoints |
|---|---:|---:|---:|---:|
| Qwen | 40/40 | 3,188 | 291.5 ms | 0 / 0 |
| Whisper large-v3 | 40/40 | 2,027 | 464.2 ms | 0 / 0 |
| Whisper Turbo | 40/40 | 1,087 | 709.0 ms | 0 / 0 |

All three proofs report `ok=true`, with no monitoring error. The guards were active before backend imports. These are completed **personal-corpus** proofs, not completed 288-WAV synthetic evaluations for these candidates. They do not supply realtime EOS timings.

No network adapter or firewall was changed: this is **not physical network isolation**. Python audit hooks do not cover every native extension entry point, and endpoint/process polling can miss connections or descendants born and gone between samples. The sample counts and largest gaps above make that limitation explicit.

## Reproducibility and validation

Native Windows Python 3.12 uses the isolated `C:\dev\voicebar\.venv-stt` environment. Saved pins include Torch `2.9.1+cu128`, Transformers `5.19.0`, Hugging Face Hub `1.33.0`, Accelerate `1.15.0`, faster-whisper `1.2.1`, CTranslate2 `4.8.2`, NumPy `2.5.3` and CPU ONNX Runtime `1.31.0` for Silero. The complete freeze is `requirements-stt-candidates.txt`; Torch comes from the official CUDA 12.8 wheel index. The editable checkout line is machine-specific. Do not sync this environment from the CPU project lockfile. `Stage3-Candidate-Methods.md` contains the reconstruction commands and official API sources.

| Local asset | Repository | Pinned revision |
|---|---|---|
| Qwen | `Qwen/Qwen3-ASR-1.7B-hf` | `bcd2b5b7f32b480ab5790554cfa8347f246a14f3` |
| Whisper large-v3 | `Systran/faster-whisper-large-v3` | `edaa852ec7e145841d8ffdb056a99866b5f0a478` |
| Whisper Turbo | `dropbox-dash/faster-whisper-large-v3-turbo` | `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` |

Model sizes and SHA-256 hashes are recorded in `models/manifest.json`; repeating setup verified existing assets and downloaded nothing. CUDA runs refuse silent CPU fallback and record actual model placement. Qwen forces English and deterministic greedy decoding; Whisper forces English transcription, beam five, temperature zero and previous-text conditioning off. Vocabulary comes only from the existing catalog. Whisper reports its 223-token hint cap, with configured aliases, alias targets and sites prioritized. Benchmark checkpoints bind source, package versions, model identity, selected session and recording provenance.

Verified in `stage3-candidates-validation.json`: **916 non-window tests passed in 24.17 seconds**, lint passed, and the parser contract remains byte-identical to the seed (`ded62ba9134934237bbb2c2e5fe2ee064503aaa7285ac61a9b1a7e43dcd943eb`). A fake PTT cleanup test was corrected to synchronize completion and use a controlled deadline clock; its production safety timeout and closure assertions were preserved. An earlier checkpoint-replacement access error passed in isolation; persistence code was not changed without a reproduced cause.

Not yet verified: a passing 90% personal gate, approved aliases, backend selection, live microphone/PTT actions, the corrected-cue quiet recording pass, a fresh real-window integration run, or a completed new-candidate 288-WAV synthetic comparison. Stage 4 always-on listening and Stage 5 UI remain queued. This interim document was assembled from saved evidence; drafting it ran no recognition, tests, microphone capture, hotkeys, windows or actions.

Evidence files are under `C:\dev\voicebar\docs\evidence`: the three `stage3-candidates-*-cuda-realtime.json` reports, three `stage3-candidates-offline-*-cuda.json` proofs, two `stage3-candidates-*-cpu-awake-probe.json` checks, the incomplete Turbo CPU checkpoint, `stage3-candidates-power-events.json`, `stage3-candidates-awake-checks.json`, `stage3-candidates-environment.json` and `stage3-candidates-validation.json`.
