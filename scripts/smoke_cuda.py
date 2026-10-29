"""Decode one local speech WAV and prove real Parakeet CUDA node execution.

Run this in the isolated GPU environment, never through `uv run` (which syncs
the CPU project's dependency set). This script does not download any assets.
"""

import argparse
import json
import platform
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"ok": False, "backend": "parakeet", "device": "cuda",
              "python": sys.version, "platform": platform.platform(),
              "physical_network_disabled": False,
              "monitoring_limit": "Python socket audit guard only in this smoke; use prove_offline.py "
                                  "for native IPv4/IPv6 endpoint sampling of the corpus."}
    backend = None
    policy = None
    try:
        from voicebar.stt.offline import SocketAuditPolicy
        policy = SocketAuditPolicy()
        report["socket_guard_canaries"] = policy.install_and_check()
        try:
            cpu_ort = version("onnxruntime")
        except PackageNotFoundError:
            cpu_ort = None
        if cpu_ort is not None:
            raise RuntimeError("The GPU environment also contains CPU onnxruntime; recreate it with only "
                               "onnxruntime-gpu because both distributions share a namespace.")
        if version("onnxruntime-gpu") != "1.31.0":
            raise RuntimeError("This CUDA proof requires pinned onnxruntime-gpu 1.31.0.")
        from voicebar.catalog import load_catalog
        from voicebar.stt.base import transcribe_wav
        from voicebar.stt.models import offline_environment, require_model, sha256
        from voicebar.stt.parakeet import ParakeetBackend
        offline_environment()
        import onnxruntime as ort
        report["packages"] = {name: version(name) for name in
                              ("onnxruntime-gpu", "onnx-asr", "numpy", "nvidia-cudnn-cu13")}
        report["ort_build_info"] = ort.get_build_info()
        report["wav"] = str(args.wav.resolve())
        report["wav_sha256"] = sha256(args.wav)
        report["model_manifest_sha256"] = sha256(args.root / "models/manifest.json")
        directory = require_model(args.root, "parakeet-cuda", verify_hashes=True)
        vad_directory = require_model(args.root, "silero", verify_hashes=True)
        catalog = load_catalog(args.root).catalog
        began = time.perf_counter()
        backend = ParakeetBackend(directory, vad_directory, catalog, device="cuda",
                                  profile_directory=output.parent / (output.stem + "-profiles"))
        report["cold_load_ms"] = (time.perf_counter() - began) * 1000
        report["recognition"] = transcribe_wav(backend, args.wav)
        report["placement"] = backend.collect_profiles()
        if not report["recognition"]["text"]:
            raise RuntimeError("The smoke WAV produced no speech; CUDA decoder execution was not exercised.")
        for name in ("encoder", "decoder_joint"):
            if report["placement"][name]["cuda_compute_nodes"] == 0:
                raise RuntimeError(f"No real CUDA compute nodes executed in Parakeet {name}.")
        if any(event["phase"] != "canary" for event in policy.events):
            raise RuntimeError("A runtime socket operation was refused during the CUDA smoke.")
        report["ok"] = True
    except Exception as error:  # noqa: BLE001 -- CLI boundary records a failed proof and returns nonzero.
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if backend is not None:
            backend.close()
        if policy is not None:
            report["socket_refusals"] = policy.events
        output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("ok", "backend", "device")}
                     | {"evidence": str(output), "error": report.get("error")}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
