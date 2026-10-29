# Reproduce the Windows GPU benchmark

The ordinary `uv run` environment uses CPU ONNX Runtime. CUDA benchmarking uses a
separate `.venv-gpu` so CPU and GPU distributions never overwrite the same ORT
module. `requirements-cuda.txt` records the exact installed GPU environment used
for this machine's results. Installing packages and downloading models require
network access once; runtime loads only hashed local assets.

In native Windows PowerShell, from `C:\dev\voicebar`:

```powershell
uv run python scripts/setup.py --cuda
uv venv --python .venv\Scripts\python.exe .venv-gpu
uv pip install --python .venv-gpu\Scripts\python.exe -r requirements-cuda.txt
$env:PYTHONPATH = (Get-Location).Path
.venv-gpu\Scripts\python.exe scripts/smoke_cuda.py --help
.venv-gpu\Scripts\python.exe scripts/prove_offline.py --backend parakeet --device cuda --manifest fixtures/synthetic/manifest.json --output docs/evidence/offline-parakeet-cuda-new.json
.venv-gpu\Scripts\python.exe scripts/benchmark_stage3.py --backend parakeet --device cuda --subset mine --timing realtime --output docs/evidence/mine-parakeet-cuda-new.json
```

Use a new output name for each new benchmark; `--resume` requires identical
dataset, code and model fingerprints. Do not run `uv sync` in `.venv-gpu`, since
the main project's lock describes the CPU environment. Recognition never plays
audio, opens a microphone, or executes a plan. GPU PTT remains unavailable from
the main CPU environment until a backend passes the personal accuracy gate.

The adapter calls the installed ORT `preload_dlls(directory="")` to load its NVIDIA
wheel DLLs. It uses Parakeet's float32 export and HEURISTIC cuDNN search; the CPU
candidate uses the int8 export from the same model repository revision. This
comparison measures the usable configurations, rather than isolating hardware
speed with identical numerical precision. NumPy preprocessing, Silero VAD and
some ONNX shape operators remain on CPU.

The RTX 5070 Laptop / driver 592.82 actually executed 3,026 CUDA encoder compute
nodes and 460 CUDA decoder compute nodes in `stage3-cuda-smoke.json`. Provider
registration alone was not used as proof. The smoke also retained CPU shape/copy
operations and ORT warnings. The complete CUDA offline proof checked all 288 WAVs.

See `docs/STAGE3-GATE.md` for results and limitations. Current personal accuracy
fails the gate, so the configured backend remains null.

Checked sources: [ORT CUDA provider and DLL loading](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html),
[Parakeet ONNX exports](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx),
[CUDA minor version compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html).
