# Stage 8 local runtime evidence

The socket proof starts a disposable Voicebar desktop app, loads a local speech
backend once, and plays a scripted WAV session into the normal microphone/held
capture boundary. It exercises capture closure, recognition, gate, parser,
recorded execution decisions, UI feedback and JSON logging. It samples the app
process and discovered descendants with psutil, refuses listening sockets and
UDP, and permits TCP clients only to exact `127.0.0.1`. A Python audit guard
rejects bind, nonlocal connect and DNS before their CPython system calls.

The executor records plans so the recordings cannot operate user applications.
The proof does not register global keyboard shortcuts. These are deliberate
boundaries of this test; they are not full native-action or hotkey evidence.
Independent already-running Ollama/Fusion servers are not children of Voicebar
and are outside the monitored process tree.

Verified:

- The selected **Qwen CUDA** backend completed the real GUI session in
  `docs/evidence/stage8-network-qwen-final.json`: **4/4 expected decisions**,
  **69.51 s** including cold loading/warmup/replay/shutdown, **1,309 psutil
  samples** over two owned PIDs, and **zero forbidden endpoints or runtime
  audit attempts**. Maximum actual sampling gap was **101.78 ms**, so this
  remains sampled observation plus Python audit refusal. The microphone
  substitute closed at shutdown. Physical networking remained enabled.
- `tests/test_stage8_network.py` starts the real controller in an owned child
  with WAV input and a deterministic fixture recognizer. It requires no model
  downloads. It also tests exact loopback policy, bind refusal, offline-state
  refusal and the telemetry/update switches.
- The real GUI/speech run in
  `docs/evidence/stage8-network-moonshine.json` completed four WAV files with
  Moonshine on CPU, 296 psutil samples, zero forbidden observed endpoints and
  zero runtime audit refusals. Maximum actual gap was 260.63 ms despite the
  requested 10 ms cadence. Physical networking remained enabled.
- The latest-controller Moonshine replay in
  `docs/evidence/stage8-network-moonshine-final.json` also completed all four
  WAVs without a forbidden endpoint or audit attempt. Its 135 samples had a
  **2,914.02 ms** maximum gap while other work ran. That coarse observation is
  retained rather than presented as continuous socket coverage. The sampler
  was then changed to query the system endpoint tables once per sample and
  traverse each connected process subtree once, including retained orphans.
- New and existing socket tests, the helper restoration mock and native-runtime
  preflight/telemetry checks: **44 passed in 7.22 s**. Windows endpoint
  queries need normal host process visibility; the execution sandbox initially
  denied them. No test or sampling threshold was relaxed.
- Static distribution audit searched **61 installed distributions, 9,762 Python
  source files**, and produced **3,484 review hits**. The dependency tree and
  per-distribution counts are in
  `docs/evidence/stage8-dependency-summary.json`. Reproduce full source hits with
  `python scripts/audit_stage8_dependencies.py --output work/stage8-dependency-audit.json`.

Not verified by these results:

- The user declined the physical networking-off run. Its gate remains
  unverified. No adapter or firewall was changed; no further adapter request
  or toggle will be made in this session.
- Polling can miss a brief endpoint or process entirely between samples. Audit
  hooks cannot intercept native code that calls Winsock directly. Neither
  mechanism is a firewall or a universal proof about unexercised code paths.
- Native extension internals were not statically audited. The source scan is
  a conservative token search with false positives, not a supply-chain audit.

## Dependency review and controls

Every version below was read from the installed package metadata and the listed
APIs/flags were inspected in installed source or binding documentation.

| Installed component | Finding | Runtime control |
| --- | --- | --- |
| huggingface-hub 1.33.0 | `utils/_telemetry.py` can enqueue outbound telemetry; CLI has update checks; model helpers can fetch lazily | `HF_HUB_DISABLE_TELEMETRY=1`, `HF_HUB_DISABLE_UPDATE_CHECK=1`, `DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1` before optional model imports; runtime additionally sets `HF_HUB_OFFLINE=1` |
| transformers 5.19.0 | `from_pretrained` can fetch config, tokenizer and weights; Hub supplies telemetry user agent | Absolute manifest-validated local paths, `local_files_only=True`, `trust_remote_code=False`, `TRANSFORMERS_OFFLINE=1`; no training/tracking integrations are started |
| ONNX Runtime 1.31.0 | Installed binding exposes platform telemetry; packaged tools include download helpers | `disable_telemetry_events()` before any Voicebar session; packaged download/benchmark tools are not called |
| onnx-asr 0.12.0 | `resolver.py` can download a named model if local resolution fails | Existing explicit local directories plus Hub offline mode; missing assets fail before backend construction |
| moonshine-voice 0.1.5 | `download.py`/`download_file.py` fetch default models when convenience download functions are called | Voicebar supplies an existing manifest-backed model directory directly to `Transcriber`; runtime never calls its downloader |
| torch 2.9.1+cu128 | `hub.py`/model-zoo utilities can download; distributed elastic metrics and compiler instrumentation contain telemetry terms | Voicebar uses local Transformers weights and eager inference, never hub/model-zoo/download, distributed training or compiler telemetry entrypoints |
| requests, httpcore, urllib3, fsspec | Generic HTTP/client implementations exist transitively | No Voicebar runtime caller except the separate fixed-loopback clients described below; Hub offline refusal prevents the model-loader paths |
| Remaining packages | No additional autonomous runtime update or telemetry path found in reviewed source hits | False positives include `comtypes`/protobuf local version compatibility checks, lexer keywords, generated `...ArgsEntry` names and ordinary HTTP parser comments; these do not need network switches |

`voicebar/__init__.py` sets tracking/update opt-outs early without setting
offline mode, so the explicitly invoked setup script can still download models.
`voicebar.stt.models.offline_environment()` enables offline mode without importing
model runtimes. `disable_onnx_telemetry()` runs in each Python ONNX session
constructor before session creation. A missing-asset factory preflight therefore
still fails before importing the native runtime. Setup remains the only supported
model-download path.

Ollama's client in `voicebar/resolver/ollama.py` has a fixed numeric loopback
host, validates that the selected model is local, has no pull endpoint, and
does not follow redirects or use proxies. Stage 7's existing Fusion bridge in
`voicebar/fusion_mcp.py` also uses fixed `127.0.0.1:27182`, starts no server or
process, and does not follow redirects or proxies. This is an additional local
app client beyond the older hard-rule wording that mentioned only Stage 6;
Stage 8's explicit socket acceptance permits exact `127.0.0.1` destinations.

## Reproduce online and physically offline

The manifest uses `{"items":[{"id":"unique","wav":"relative-or-absolute.wav"}]}`.
Choose short owned recordings (under 20 s each). Run with the actual configured
backend/device, for example:

```powershell
.venv\Scripts\python.exe scripts\prove_stage8_local.py --manifest work\stage8-session.json --backend qwen --device cuda --output docs\evidence\stage8-network-qwen.json
```

Only after explicit user approval, an administrator can run the prepared helper:

```powershell
powershell.exe -NoProfile -File scripts\run_stage8_offline.ps1 -ProjectRoot C:\dev\voicebar -AdapterName Wi-Fi -Manifest work\stage8-session.json -Backend qwen -Device cuda
```

The prepared helper has not been executed. An isolated test with fully mocked
adapter/process commands verified that a child-launch failure restores only
the originally-up Wi-Fi adapter and records restoration evidence. The production
script retains its elevation requirement.

The helper refuses another active physical adapter, disables only the exact
previously-up Wi-Fi adapter, runs the same manifest with `--expect-offline`,
and restores Wi-Fi in `finally`. The proof verifies all physical adapters before
and after, and psutil verifies their down state during each sample. It records
manifest SHA-256 so the online/offline sessions can be matched. A bounded
timeout cleans only the helper-owned process tree and still runs restoration.
The restoration evidence is `docs/evidence/stage8-adapter-restoration.json`.

## Firewall commands are printed, never run

```powershell
New-NetFirewallRule -DisplayName 'Voicebar block outbound' -Direction Outbound -Program 'C:\dev\voicebar\.venv\Scripts\python.exe' -Action Block -Profile Any
```

This machine's venv executable is a launcher. psutil verified that its actual
process image is
`%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe`.
Blocking the launcher alone does not block the interpreter it spawns. The proof
also prints optional rules for that image and `pythonw.exe` for Startup. Those
rules affect **all projects using that shared interpreter**. No firewall rule
has been installed by Voicebar or this proof.
