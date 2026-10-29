# Stage 11 latency audit — 2026-10-26

The observed delay has two distinct causes: roughly one second of speech recognition for ordinary commands, and repeated cold model loading when visual fallback is needed. Faster installed speech models have not preserved the accepted command accuracy. A complete isolated comparison preserved accuracy at four CPU threads but did not establish a consistent speed improvement. Releasing unused speech-model GPU cache preserved accuracy and recovered approximately 2.13 GiB for other local models.

## Observed live session

Source: `logs/listen-20261026T022626Z-1201a7e0.jsonl`. The table includes the 20 nonempty decision records. Percentiles use nearest rank; the maximum is separate so the first-command outlier remains visible.

| Measurement | Count | Median | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Speech recognition | 20 | 963.7 ms | 1,243.3 ms | 3,907.5 ms |
| Recognition queue | 20 | 1.1 ms | 4.4 ms | 39.5 ms |
| End-of-speech wait | 20 | 320.0 ms | 320.0 ms | 320.0 ms |
| Gate | 20 | 10.2 ms | 17.3 ms | 24.7 ms |
| Speech end to action start | 7 | 1,380.1 ms | 4,281.5 ms | 4,281.5 ms |
| Speech end to final result | 20 | 1,534.9 ms | 6,736.1 ms | 6,845.9 ms |

The first real command took 3,907.5 ms in recognition despite startup warmup. The next recognition took 807.7 ms. The queue and gate do not explain the ordinary one-second delay. Opening Calculator added 934.4 ms of native execution; a later successful calculation added 347.6 ms. Those execution costs are distinct from recognition.

Four visual fallbacks spent 5,198.5–5,436.2 ms in screen resolution and timed out. Their complete user-visible delays were approximately 6.4–6.8 seconds. Four other unresolved commands left screen resolution in under 1 ms; those are parser/targeting failures, not slow vision inference.

## Why the native visual probe was fast but the live commands timed out

Sources: `.cache/stage10-ollama-portable-stdout.log` and `.cache/stage10-ollama-portable-stderr.log`. Server timestamps below are local EDT on October 25; the corresponding listening log uses UTC on October 26.

- At 22:28:49 the successful warmed visual request took 745.67 ms at the server; the native probe measured approximately 784 ms including client overhead. The model had all 29 layers offloaded to the GPU. Its image encoder took approximately 153 ms across the logged batches, and the selected response took approximately 64 ms to decode.
- The next visual request began at 22:34:20, 331 seconds later. This exceeds the five-minute residency setting used by that launched version. The server started loading the visual model again.
- At 22:34:21 the server reported only about 1.0 GiB available GPU memory. At 22:34:26 GPU discovery hit its five-second watchdog. At 22:34:27 the server explicitly disabled multimodal-projector GPU offload because of limited VRAM and launched with `--no-mmproj-offload`.
- Each subsequent attempt again had only 1.5–1.6 GiB free, approximately 1.1 GiB available after the server reserve. Every attempt ended with `client connection closed before llama-server finished loading, aborting load`; the server returned HTTP 499. These requests did not reach normal selection inference before their five-second client deadlines.

This establishes model residency and memory fitting as the immediate timeout cause. The log does not establish which other process owned every missing byte of GPU memory. In particular, allocating/caching more speech tensors after the first real audio is plausible, but there is no contemporaneous per-process allocation series proving that attribution.

The runtime also preloaded the optional text resolver alongside vision. Its logged load used approximately 915 MiB of GPU model weights, 120 MiB of GPU KV cache and 193 MiB of GPU compute buffer, with the remaining weights on the host. Those allocations compete with speech and vision even when deterministic routing is sufficient. Avoiding unnecessary concurrent residency and retaining the warmed visual model are direct fixes to validate. Increasing the five-second command deadline would hide the reload problem without fixing it.

The Stage 11 implementation is being changed concurrently; the observations above describe the logged launched version, not a claim that its old residency setting remains in current source.

## Installed speech alternatives and accuracy

All completed results below use the same 40 repaired-cue recordings from `20261010T165301Z-check-0ffa4a3f`, unchanged expected plans, and the historical paced replay timing method. They are not live microphone measurements.

| Configuration | Exact plans | Historical speech-end p50 / p95 |
| --- | ---: | ---: |
| Qwen CUDA, grammar/catalog context | 35/40 | 1,419 / 2,538 ms |
| Qwen CUDA, catalog-only context | 33/40 | 2,044 / 4,566 ms |
| Whisper large CUDA | 30/40 | 1,219 / 2,128 ms |
| Whisper turbo CUDA | 28/40 | 535 / 1,032 ms |
| Parakeet CUDA | 28/40 | 242 / 485 ms |
| Parakeet CPU | 26/40 | 181 / 460 ms |
| Moonshine small CPU | 24/40 | 4,201 / 9,220 ms |

The saved transcripts were reparsed against the current catalog/parser during this audit; all seven scores were unchanged. No model inference or action execution was needed for that comparison. The accepted Qwen result remains 35/40, with one null plan and four nonmatching non-null plans, including payload formatting differences.

The fastest CPU model's errors are not limited to refusal. It produced `Ctrl+Shift+D` for an expected `Ctrl+Shift+T`, and incorrect search payloads. A fast recognizer followed by a slower recognizer only when parsing returns null would still execute those incorrect non-null results. Such a cascade needs its own accuracy and false-activation evaluation; the historical data does not justify enabling it.

The saved Whisper turbo CPU report has only six results and is incomplete (approximately 29.6/46.6 seconds p50/p95). Unfinished Moonshine medium/CPU probes are likewise not complete alternatives. Historical synthetic accuracy is not a substitute for this user's recordings.

Evidence files: `docs/evidence/stage3-qwen-command-context-cuda-realtime.json`, `stage3-candidates-qwen-cuda-realtime.json`, `stage3-candidates-whisper-large-cuda-realtime.json`, `stage3-candidates-whisper-turbo-cuda-realtime.json`, and `stage3-recheck-{parakeet-cpu,parakeet-cuda,moonshine-cpu}-realtime.json`.

## Qwen optimization experiment

The installed environment reports Torch 2.9.1+cu128, 16 intra-op threads and 16 inter-op threads by default. Its installed `set_num_threads` documentation requires applying the intra-op setting before eager/JIT/autograd work. The CPU path already sets four threads; the CUDA path had retained the default. The experiment changes only intra-op threads, leaving inter-op threads, model, BF16 placement, attention implementation, complete vocabulary, generation settings and expected plans unchanged.

`scripts/probe_stage11_speech.py` runs two fresh processes sequentially, first 16 threads and then 4. Each validates the complete recording manifest and WAV hashes, performs the existing zero-audio startup warmup, then decodes all 40 recordings on a single recognition worker. It records loading and warmup separately, marks the first real recording separately, compares every transcript and exact plan, and rejects comparisons with changed source, vocabulary or recording hashes. No microphone, gate, executor, native window or desktop capture is used.

The experiment is final-only, unpaced recognition, matching the application's recognition path after segmentation. Its `stt_ms` must not be labeled speech-end latency or compared directly with the paced historical table. First-command latency and the remaining 39 recordings are reported separately. Because the 16-thread pass precedes the 4-thread pass, thermal/order effects remain a limitation; a marginal difference would need a reversed confirmation before adoption.

Raw evidence is written to `docs/evidence/stage11-qwen-threads-16threads.json` and `stage11-qwen-threads-4threads.json`; paired results are written to `stage11-qwen-threads.json`.

Both complete runs produced 35/40 exact plans, with all 40 transcripts and plans identical between settings. The current grammar generated the same 1,664-token prompt in both runs, SHA-256 `35a2800b2a0f3bb3c78c0dd033a30ecb4e19affdf1a37fa382b05ed4bdf4d45a`.

| Setting | Exact plans | First real recording | All 40 p50 / p95 | Remaining 39 p50 / p95 |
| --- | ---: | ---: | ---: | ---: |
| 16 threads | 35/40 | 1,730.0 ms | 1,711.7 / 2,613.1 ms | 1,693.3 / 4,115.8 ms |
| 4 threads | 35/40 | 1,595.0 ms | 1,618.6 / 2,743.7 ms | 1,630.4 / 3,916.2 ms |

The median paired saving across recordings 2–40 was **-10.0 ms**: the typical matched recording was slightly slower with four threads. Large gains were concentrated late in the pass, and the all-recording p95 regressed. This result does not justify adopting four threads as a speed fix. The change in p95 between sample counts 40 and 39 follows the documented nearest-rank definition; no observations were dropped from the exact-plan score.

The 16-thread process ended with 4,095,881,728 bytes of live CUDA allocation, 6,406,799,360 bytes reserved by Torch, and only 886,046,720 bytes free on the GPU. Peak live allocation was 4,849,106,432 bytes. Thus **2,310,917,632 bytes (2.15 GiB)** were reserved but not used by live tensors after recognition. This reproduces a mechanism that can prevent visual-model loading even without concurrent model inference. It does not retroactively establish the ownership of every allocation in the earlier live session.

## Measured cache release and implementation

The installed `torch.cuda.empty_cache` API explicitly releases unoccupied allocator cache for other GPU applications while retaining live tensors. The third complete 40-recording pass, `docs/evidence/stage11-qwen-release-cache.json`, tested release after startup warmup and every completed recognition at the unchanged 16-thread setting. Its timing includes cache-release overhead. Source, vocabulary, WAV and expected-plan hashes matched the baseline; all **40 transcripts and plans were identical**, retaining **35/40 exact plans**.

| Measurement | Baseline | Cache release |
| --- | ---: | ---: |
| Reserved CUDA memory after recognition | 6,406,799,360 bytes | 4,116,709,376 bytes |
| Live CUDA tensors | 4,095,881,728 bytes | 4,095,881,728 bytes |
| Peak live CUDA tensors | 4,849,106,432 bytes | 4,851,056,128 bytes |
| GPU free memory after recognition | 886,046,720 bytes | 3,176,136,704 bytes |
| All 40 recognition p50 / p95 | 1,711.7 / 2,613.1 ms | 1,693.7 / 2,666.6 ms |
| First real recording | 1,730.0 ms | 1,685.9 ms |

Reserved memory decreased by 2,290,089,984 bytes (2.13 GiB). Cache release itself cost 20.8 ms median, 39.1 ms p95 and 46.3 ms maximum. The median paired total-recognition overhead was 23.1 ms. This is a memory-sharing fix, not an ASR latency improvement; the separate-pass timing/order limitations still apply.

`voicebar/stt/qwen.py` now deletes the completed utterance's input/output tensor references and releases unused cache before publishing its final transcript. Startup warmup follows that same path. Provisional prefixes keep their cache, and CPU recognition makes no CUDA cache calls. If cache release fails, recognition reports the existing `SttError` and does not publish a transcript. Live model parameters remain resident. The benchmark's explicit `--release-cache` wrapper measured the pre-integration implementation; after this source change the backend already performs the release, so subsequent ordinary benchmarks should omit that extra wrapper.

Focused verification: `python -m pytest tests/test_qwen.py tests/test_stt_context.py tests/test_stt_base.py -q --basetemp .cache/pytest-stage11-speech-cache` returned **66 passed in 2.01s**. Ruff passed for the adapter, tests and benchmark script. The regression tests verify that temporary tensor references are gone before cache release, live model parameters remain, final publication follows release, partials retain cache, warmup releases once, CPU mode is unaffected, silent segments do not allocate/release, and errors publish no transcript.

The earlier Stage 8 profile spent approximately 415 ms in prefill and 342 ms in `StopCheck.__call__` while using a 1,662-token vocabulary. Installed `transformers/generation/utils.py` implements that stop check as a GPU tensor boolean read. This synchronizes CPU and GPU, so its wall time is not evidence of 342 ms of CPU computation. A four-thread setting is an experiment, not a conclusion established by that profile. Reducing the vocabulary is a separate semantic change and must preserve accuracy before adoption.

### Later combined-model diagnosis

The first combined run (`evidence/stage11-coexistence.json`) exposed a further
problem. With an old permanently resident visual runner already present, speech
loading took 15.76 seconds and startup warmup took 187.16 seconds. After both
selectors warmed, reported GPU allocations were 4.086 GB speech, 2.412 GB text
selection and 1.989 GB vision on an 8.546 GB GPU, with **zero free bytes** before
the first recording. That recognition took 47.12 seconds. This is evidence of
overcommit and severe contention; cache release alone does not solve it.

During diagnosis the operator explicitly unloaded Voicebar's visual model.
The run then correctly failed its residency check and closed the speech model.
Its raw error says a selector was evicted, but **automatic eviction is not
established**: the missing visual runner was caused by that manual unload.
The failed report is preserved and cannot serve as a passed coexistence gate.
Subsequent work must bound model placement and repeat the full comparison.

### Native-test temporary-file diagnosis

The first full Stage 11 suite had one stale generated-command reference and
three Access Denied failures replacing temporary JSON files. Regenerating the
reference fixes the former. A focused repeat reproduced replacement errors at
different points. The unchanged production save functions were then compared
in four fresh blocks per location and function, 500 writes per block, stopping
each block at its first error. Workspace `.cache` failed all four benchmark-save
blocks and one recording-save block. Normal Windows Temp passed all 4,000 writes.
Targets had Archive attributes, not ReadOnly. No retries, delays, service changes
or writer changes were used. This establishes directory-specific contention,
but does not identify the process responsible. The full gate should use a fresh
normal Windows Temp directory, with its tests and thresholds unchanged.
See `evidence/stage11-file-replacement.json` and the preserved diagnostic source.

### Rejected memory configurations

The same deadlines and reviewed fixtures were retained. These experiments did
not change production defaults, and their recognition/selection outputs were
never executed as desktop actions.

| Experiment | Result | Decision |
| --- | --- | --- |
| Smaller 0.6B speech model | 30/40 versus 35/40; 992 ms median decode | Reject: command accuracy regressed |
| Text-only int8 speech weights | 34/40; new wrong payload and refusal; 2,154 ms median decode | Reject: worse accuracy and speed |
| Text-only NF4 speech weights | 34/40; new wrong payload and refusal; 3,135 ms median decode | Reject: worse accuracy and speed |
| CPU-only text selector | 27/60; 28 timeouts at 1,500 ms | Reject |
| Text selector with 20 GPU layers | 46/60; 9 timeouts; 757 ms median, 1,501 ms p95 | Reject: below the unchanged 85% gate |
| Vision with 16 GPU layers | CPU image projector; actual image exceeded five seconds | Reject |

The 20-layer text selector occupied 1,500,564,684 GPU bytes. Its complete
80-fixture pass still had zero actions on the 20 noncommands, but that does not
compensate for missed commands. Evidence: `stage11-partial-selector-20layers.json`.
The int8 comparison checked all quantized text modules and retained the audio,
projection and output-head modules at BF16 on CUDA. Lower memory use alone is
not a sufficient reason to deploy it. Evidence: `stage11-int8-speech-r2.json`.
NF4 reached 2.004 GB live allocation and 2.759 GB peak, but its p95 recognition
was 5,082 ms. Both quantized candidates use experimental dependencies isolated
under `.cache`; the application environment and default recognizer are unchanged.
Evidence: `stage11-nf4-speech.json`.

## Verified

- Read and correlated the live JSONL timings with native server loading, memory and request logs.
- Reparsed all saved transcripts in seven complete 40-recording comparisons; no faster candidate recovered the accepted score.
- Confirmed installed thread-control and CUDA memory-accounting APIs from the installed package, without loading a model during the read-only audit.
- Validated the existing personal recording manifests and hashes; the repaired-cue comparison still has all 40 recordings.
- Completed the isolated 16-versus-4 thread comparison and full cache-release comparison: 120 recognitions, identical transcripts/plans across settings, 35/40 exact expected plans in each.
- Implemented final CUDA cache release and passed 66 focused tests plus Ruff without GPU inference during the code verification.

## Not verified by this audit

- The exact ownership of all GPU allocations at the time of the failed live requests.
- A new installed speech backend that meets the accepted accuracy, or any improvement from switching models.
- Sustained live microphone timing after the Stage 11 residency and recognition changes.
- Simultaneous live speech/vision residency after cache release; the isolated allocation result does not replace that integration test.
- Zero false actions from a new recognition cascade; none has been enabled by this audit.
- The complete Stage 11 release gate; this document is an investigation and experiment record, not a passed-gate claim.
