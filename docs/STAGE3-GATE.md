# Stage 3 closed with user acceptance — 2026-10-10

The user accepted **35/40 exact plans (87.5%)** and requested an end to testing after reporting laptop crashes. Stage 3 is closed as an accepted implementation checkpoint with the limitations below. This is an explicit acceptance of the accuracy shortfall, not a claim that the original 90% gate or every original gate check passed. No more model benchmarks, inference, microphone, hotkey or window tests are authorized by this closeout.

No scoped Voicebar benchmark/proof/probe processes remained at closeout. The cause of the reported crashes has not been established, and crash stability has not been verified. No power settings, drivers, services or startup entries were changed during closeout.

## Accepted result

Selected benchmark configuration: Qwen3-ASR 1.7B, CUDA BF16, fixed `desktop-grammar-catalog-v1` vocabulary context. All 40 repaired-cue recordings from `20261010T165301Z-check-0ffa4a3f` were evaluated; the user confirmed usual background noise. The same expected plans and raw text comparison were retained. No parser thresholds, contract expectations or aliases were changed to raise the score.

| Completed configuration | Exact plans | Speech end to final p50 | Speech end to final p95 |
| --- | --- | --- | --- |
| **Qwen / CUDA, grammar + catalog v1 — accepted** | **35/40 (87.5%)** | **1,418.9 ms** | **2,538.0 ms** |
| Qwen / CUDA, catalog only | 33/40 (82.5%) | 2,044.0 ms | 4,565.5 ms |
| Whisper large-v3 / CUDA | 30/40 (75%) | 1,218.5 ms | 2,128.5 ms |
| Whisper large-v3 turbo / CUDA | 28/40 (70%) | 535.5 ms | 1,032.2 ms |
| Parakeet / CUDA | 28/40 (70%) | 241.8 ms | 485.0 ms |
| Parakeet / CPU | 26/40 (65%) | 181.0 ms | 460.2 ms |
| Moonshine Small Streaming / CPU | 24/40 (60%) | 4,201.2 ms | 9,219.8 ms |

The accepted run has 40 successful results, zero inference errors, one null plan, and no missing or early finals. Speech ends are estimated by a common Silero reference at 32 ms frame resolution. These are paced saved-WAV replay timings, not measured live microphone-to-action times. First inference is included and cold loading is separate. The 300 ms p95 goal was not met. Accepted-run cold load was approximately 37.7 seconds, plus 4.3 seconds asset validation. Release-to-final p50/p95 were 1,314.2/2,396.0 ms.

The accepted context uses only live grammar vocabulary and the catalog, never recording answers. Its prompt SHA-256 is `7f3070759a1eaaa9034a047fd6c42653d6f5b62bfc19086f4ebc7a7abe00431c`; the accepted report fingerprint is `9f00e3cb44d0e9328c3cdfce3cbf6ab78ef3bb778a7a716dcffcef917b385909`. The measured context source was restored and checked against the accepted report's source hash. The unfinished phrase-v2 experiment and its tests were archived outside active test discovery; its incomplete checkpoint is not a result.

## Five remaining mismatches

| Recording | Difference |
| --- | --- |
| p015 | Typed text gained punctuation and changed `pm` to `p.m.` and capitalization. |
| p029 | Search query `lofi hip hop` became `lo-fi hip hop`. |
| p030 | Search command was misheard and routed to Google instead of YouTube. |
| p031 | Typed `hello world` became `Hello World`. |
| p032 | `write` was heard as `Right,`, so the plan was null. |

Three of five mismatches concern payload formatting; two affect command recognition/routing. They remain failures under exact comparison. No new app-name alias is needed to account for these five, and no aliases were added.

## Verified before testing was stopped

- Stage 3 interfaces, local-model loading, Windows recording, WAV-to-plan CLI, PTT controller, hotkey registration and fake-device tests are implemented.
- The accepted fixed-context source previously passed its focused 69-test run. Later shared registry/offline changes passed a separate 138-test focused run. The earlier 916-test non-window run predates the final context and registry changes; these separate runs are not a new full-suite result.
- Original Moonshine and Parakeet offline checks each processed 288 synthetic WAVs. Additional Qwen/Whisper **catalog-only** offline checks each processed these 40 personal WAVs with no observed non-loopback endpoints, runtime socket attempts or monitor errors. Python socket guards and native endpoint polling have documented sampling limitations.
- The accepted grammar-context v1 configuration has **not** had its own offline-proof rerun. The source context is built locally; that fact is not presented as a completed offline test of the final configuration.
- Parser contract remains byte-identical to the seed. The earlier clipboard restoration repair remains included.

## Unverified and intentionally left disabled

- Laptop crash cause/stability, complete final desktop pytest gate, live PTT `open notepad / type hello world / close it`, and Windows microphone indicator behavior.
- Qwen runs in the separate `.venv-stt` candidate environment. Ordinary `uv run voicebar listen --ptt` is not configured for it. `stt.backend` remains `null`; live voice execution stays disabled. Selecting the accepted benchmark result does not enable or validate live operation.
- Longer phrase-v2, Moonshine Medium full-corpus results, and unfinished CPU probes are not accepted alternatives. No more testing or automatic restarts will run as part of this stage.
- Stages 4 and 5 remain queued. They were not started during closeout. No new recordings or manual checks are requested now.

## Evidence

Accepted result: `docs/evidence/stage3-qwen-command-context-cuda-realtime.json`.
Preserved measured source and prompt: `docs/evidence/experiment-source/stage3-qwen-command-context-cuda-realtime/`.
Closeout record: `docs/evidence/stage3-closeout.json`.
Earlier failed gate report is retained as `docs/STAGE3-INITIAL-GATE.md`; repaired-cue and catalog-only comparisons remain in `docs/STAGE3-RECHECK.md` and `docs/STAGE3-CANDIDATES.md` as historical evidence. This is not a passed-original-gate commit.
