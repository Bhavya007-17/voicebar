"""One-pass Stage 6 benchmark; reviewed local fixtures, no action execution.

API contracts: https://docs.ollama.com/api/tags and the installed client's
``/api/show`` metadata checks in ``voicebar/resolver/ollama.py``. Inventory and
inference use client connections to 127.0.0.1 only. No models are downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import re
import time
from pathlib import Path

import yaml

from voicebar.catalog import load_catalog
from voicebar.parser import parse
from voicebar.resolver.base import Resolver
from voicebar.resolver.candidates import strip_wake_name
from voicebar.resolver.ollama import OllamaBackend
from voicebar.schema import validate_plan

TIMEOUT_MS = 1500
# Teardown is outside the action deadline and is never included as model latency.
# A transport that ignores its deadline must finish before another fixture runs.
WORKER_TEARDOWN_TIMEOUT_S = 3.0
COMMAND_COUNT = 60
NONCOMMAND_COUNT = 20
MAX_RESPONSE_BYTES = 512 * 1024


class BenchmarkError(ValueError):
    """A provenance, fixture, or local inventory problem prevents benchmarking."""


def require_review(fixture_path: Path, receipt_path: Path) -> tuple[bytes, str]:
    """Require the list was shown and feedback received before model warm-up."""
    try:
        raw = fixture_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise BenchmarkError(f"User fixture review receipt is required: {error}") from error
    if (not isinstance(receipt, dict) or receipt.get("shown") is not True
            or receipt.get("feedback_received") is not True
            or receipt.get("fixture_sha256") != digest):
        raise BenchmarkError("Fixture review must have shown:true, feedback_received:true, and the exact fixture_sha256")
    return raw, digest


def validate_fixtures(raw: bytes, catalog, context: dict) -> list[dict]:
    try:
        rows = yaml.safe_load(raw.decode("utf-8-sig"))
        if not isinstance(rows, list) or len(rows) != COMMAND_COUNT + NONCOMMAND_COUNT:
            raise ValueError("Expected exactly 60 commands and 20 noncommands")
        identifiers = set()
        commands = 0
        for row in rows:
            if (not isinstance(row, dict) or set(row) - {"id", "text", "expected", "context"}
                    or not {"id", "text", "expected"} <= set(row)):
                raise ValueError("Each fixture needs id, text, expected, and optional context")
            identifier = row["id"]
            if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
                raise ValueError("Fixture ids must be unique nonempty strings")
            identifiers.add(identifier)
            fixture_context = row.get("context", {})
            if (not isinstance(fixture_context, dict) or set(fixture_context) - {"last_app"}
                    or ("last_app" in fixture_context and fixture_context["last_app"] not in catalog.apps)):
                raise ValueError(f"{identifier}: context may contain only an installed last_app")
            merged_context = {**context, **fixture_context}
            rest = strip_wake_name(row["text"], merged_context)
            if not rest:
                raise ValueError(f"{identifier}: speech must start with the configured wake name")
            if parse(rest, catalog, last_app=fixture_context.get("last_app")) is not None:
                raise ValueError(f"{identifier}: tier 1 already parses this fixture")
            expected = row["expected"]
            if expected is None:
                continue
            commands += 1
            validate_plan(expected)
            for action in expected:
                if "app" in action and action["app"] not in catalog.apps:
                    raise ValueError(f"{identifier}: app is absent from the catalog")
                if "target" in action and action["target"] not in ["active", *catalog.apps]:
                    raise ValueError(f"{identifier}: target is absent from the catalog")
                if "engine" in action and action["engine"] not in catalog.engines:
                    raise ValueError(f"{identifier}: engine is absent from the catalog")
                if "site" in action and action["site"] not in catalog.sites:
                    raise ValueError(f"{identifier}: site is absent from the catalog")
                if action["action"] == "open_url" and action["url"] not in catalog.sites.values():
                    raise ValueError(f"{identifier}: URL is absent from the catalog")
                for payload in ("text", "query"):
                    if payload in action and action[payload] not in rest:
                        raise ValueError(f"{identifier}: payload must be an exact transcript span")
        if commands != COMMAND_COUNT:
            raise ValueError("Expected exactly 60 commands and 20 noncommands")
        return rows
    except (UnicodeError, ValueError, TypeError, yaml.YAMLError) as error:
        raise BenchmarkError(f"Invalid Stage 6 fixtures: {error}") from error


def _local_request(path: str, body=None, *, timeout_s: float = 10.0) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=timeout_s)
    try:
        encoded = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        connection.request("GET" if body is None else "POST", path, body=encoded,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise BenchmarkError(f"Local inventory returned HTTP {response.status}")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BenchmarkError("Local model inventory exceeded its size limit")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise BenchmarkError("Local inventory must return an object")
        return data
    finally:
        connection.close()


def discover_models(request=None) -> tuple[list[dict], list[dict]]:
    """Use numeric /api/show metadata, never infer parameters from model names."""
    request = _local_request if request is None else request
    listed = request("/api/tags", timeout_s=10.0)
    if not isinstance(listed, dict) or not isinstance(listed.get("models"), list):
        raise BenchmarkError("/api/tags did not return a models list")
    available, excluded, seen = [], [], set()
    for entry in listed["models"]:
        if not isinstance(entry, dict):
            raise BenchmarkError("/api/tags contains a non-object model")
        name = entry.get("name", entry.get("model"))
        if not isinstance(name, str) or not re.fullmatch(r"[\w./:-]+", name):
            raise BenchmarkError("/api/tags contains an invalid model identity")
        if name in seen:
            continue
        seen.add(name)
        reason = None
        if ":" not in name:
            reason = "An explicit local model tag is required"
        elif "cloud" in name.lower() or entry.get("remote_host") or entry.get("remote_model"):
            reason = "Remote/cloud model"
        if reason:
            excluded.append({"model": name, "reason": reason})
            continue
        info = request("/api/show", {"model": name}, timeout_s=10.0)
        if not isinstance(info, dict):
            raise BenchmarkError("/api/show did not return a metadata object")
        model_info = info.get("model_info")
        count = model_info.get("general.parameter_count") if isinstance(model_info, dict) else None
        capabilities = info.get("capabilities")
        if info.get("remote_host") or info.get("remote_model"):
            reason = "Remote/cloud model"
        elif not isinstance(capabilities, list) or "completion" not in capabilities:
            reason = "No completion capability"
        elif type(count) is not int or not 0 < count <= 4_000_000_000:
            reason = "Numeric parameter count must be positive and at most 4B"
        if reason:
            excluded.append({"model": name, "reason": reason})
        else:
            available.append({"model": name, "parameter_count": count,
                              "digest": entry.get("digest"), "metadata": info})
    return available, excluded


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile, specified in report provenance."""
    return sorted(values)[max(0, math.ceil(fraction * len(values)) - 1)]


def _save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def run_benchmark(root: Path, output: Path, *, request=None, backend_factory=None,
                  timer=None, progress=None) -> dict:
    root, output = Path(root), Path(output)
    fixture_path = root / "fixtures" / "loose.yaml"
    receipt_path = root / "docs" / "evidence" / "stage6-fixture-review.json"
    raw, digest = require_review(fixture_path, receipt_path)
    if output.exists():
        raise BenchmarkError("Output already exists; preserve evidence and choose a new output path")
    catalog = load_catalog(root).catalog
    settings = yaml.safe_load((root / "config" / "settings.yaml").read_text(encoding="utf-8-sig"))
    if not isinstance(settings, dict):
        raise BenchmarkError("settings.yaml must contain a mapping")
    context = {"names": settings.get("names") or [settings.get("name", "computer")]}
    rows = validate_fixtures(raw, catalog, context)
    available, excluded = discover_models(request)
    backend_factory = OllamaBackend if backend_factory is None else backend_factory
    timer = time.perf_counter if timer is None else timer
    report = {"version": 3, "backend": "ollama", "fixture_sha256": digest,
              "fixture": str(fixture_path.resolve()), "review_receipt": str(receipt_path.resolve()),
              "timeout_ms": TIMEOUT_MS, "commands": COMMAND_COUNT, "noncommands": NONCOMMAND_COUNT,
              "worker_teardown_timeout_s": WORKER_TEARDOWN_TIMEOUT_S,
              "measurement": "One resolver attempt per fixture; wait for the worker before the next fixture. "
                             "Timed-out plans stay discarded. Busy or incomplete worker coverage is ineligible.",
              "wake_context": context,
              "percentile_method": "nearest rank: sorted values at ceil(p * n)",
              "passes_per_model": 1, "excluded_models": excluded, "models": [], "chosen_model": None}
    source_root = Path(__file__).resolve().parents[1]
    report["source_sha256"] = {
        name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        for name in ("scripts/benchmark_stage6.py", "voicebar/resolver/base.py",
                     "voicebar/resolver/candidates.py", "voicebar/resolver/eligibility.py",
                     "voicebar/resolver/ollama.py", "voicebar/parser.py", "voicebar/gate.py")
    }
    for model in available:
        result = {"model": model["model"], "parameter_count": model["parameter_count"],
                  "digest": model["digest"], "metadata": model["metadata"], "rows": [],
                  "eligible": False, "status": "warm_failed"}
        try:
            backend = backend_factory(model["model"])
            if backend.warm(timeout_s=60.0) is not True:
                raise BenchmarkError("Warm-up did not confirm the model was loaded")
        except Exception as error:  # noqa: BLE001 - retain evidence for each failed model.
            result["error"] = f"{type(error).__name__}: {error}"
            report["models"].append(result)
            _save_report(output, report)
            continue
        resolver = Resolver(backend, timeout_ms=TIMEOUT_MS)
        incomplete_reason = None
        for row in rows:
            entered = timer()
            plan = resolver.resolve(row["text"], catalog, {**context, **row.get("context", {})})
            elapsed_ms = (timer() - entered) * 1000
            outcome = resolver.last_outcome
            worker_completed = resolver.wait_idle(WORKER_TEARDOWN_TIMEOUT_S)
            measured = {"id": row["id"], "text": row["text"], "expected": row["expected"],
                        "context": row.get("context", {}),
                        "plan": plan, "elapsed_ms": elapsed_ms, "exact_plan": plan == row["expected"],
                        "outcome": outcome, "worker_completed": worker_completed}
            result["rows"].append(measured)
            if progress:
                progress(model["model"], len(result["rows"]), len(rows), measured)
            if not worker_completed or outcome == "busy":
                incomplete_reason = (
                    "Resolver worker did not finish within the bounded teardown wait"
                    if not worker_completed else "Fixture could not start because the resolver was busy"
                )
                break
        commands = [row for row in result["rows"] if row["expected"] is not None]
        noncommands = [row for row in result["rows"] if row["expected"] is None]
        latencies = [row["elapsed_ms"] for row in result["rows"]]
        correct = sum(row["exact_plan"] for row in commands)
        false_actions = sum(row["plan"] is not None for row in noncommands)
        p50, p95 = percentile(latencies, .50), percentile(latencies, .95)
        completed_rows = sum(row["worker_completed"] and row["outcome"] != "busy" for row in result["rows"])
        coverage = completed_rows / len(rows)
        timeout_count = sum(row["outcome"] == "timeout" for row in result["rows"])
        busy_count = sum(row["outcome"] == "busy" for row in result["rows"])
        guarded_noncommands = sum(row["outcome"] == "non_request" for row in noncommands)
        selection_latencies = [row["elapsed_ms"] for row in result["rows"]
                               if row["outcome"] not in {"non_request", "skipped", "no_candidates", "busy"}]
        complete = completed_rows == len(rows) and busy_count == 0
        result.update(status="complete" if complete else "incomplete",
                      attempted_rows=len(result["rows"]), completed_rows=completed_rows, coverage=coverage,
                      measured_commands=len(commands), measured_noncommands=len(noncommands),
                      timeout_count=timeout_count, busy_count=busy_count,
                      guarded_noncommands=guarded_noncommands,
                      selection_rows=len(selection_latencies),
                      selection_p50_ms=percentile(selection_latencies, .50) if selection_latencies else None,
                      selection_p95_ms=percentile(selection_latencies, .95) if selection_latencies else None,
                      exact_commands=correct, accuracy=correct / COMMAND_COUNT,
                      false_actions=false_actions, p50_ms=p50, p95_ms=p95,
                      unresolved=sum(row["plan"] is None for row in commands),
                      eligible=(complete and false_actions == 0 and correct / COMMAND_COUNT >= .85 and p95 <= TIMEOUT_MS))
        if incomplete_reason:
            result["error"] = incomplete_reason
        report["models"].append(result)
        _save_report(output, report)
        unload = getattr(backend, "unload", None)
        if unload is not None:
            try:
                # Only one runtime model is configured. Release comparison
                # models so later candidates get the same memory budget.
                unload(timeout_s=30.0)
                result["unloaded"] = True
            except Exception as error:  # noqa: BLE001 - stop comparisons on uncertain cleanup.
                result.update(eligible=False, status="unload_failed",
                              error=f"{type(error).__name__}: {error}")
                _save_report(output, report)
                break
            _save_report(output, report)
    qualified = [model for model in report["models"] if model["eligible"]]
    if qualified:
        chosen = min(qualified, key=lambda model: (-model["accuracy"], model["p95_ms"], model["model"]))
        report["chosen_model"] = chosen["model"]
    _save_report(output, report)
    return report


def table(report: dict) -> str:
    lines = ["| Backend / model | Exact commands | False actions | Warm p50 | Warm p95 | Coverage | Timeouts | Eligible |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for model in report["models"]:
        if model["status"] == "warm_failed":
            cells = [f"ollama / {model['model']}", "warm-up failed", "—", "—", "—", "0/80", "—", "no"]
        else:
            cells = [f"ollama / {model['model']}",
                     f"{model['exact_commands']}/60 ({model['accuracy']:.1%})"
                     + (" incomplete" if model["status"] != "complete" else ""),
                     f"{model['false_actions']}/{model.get('measured_noncommands', 20)}",
                     f"{model['p50_ms']:.1f} ms", f"{model['p95_ms']:.1f} ms",
                     f"{model.get('completed_rows', 80)}/80", str(model.get("timeout_count", 0)),
                     "yes" if model["eligible"] else "no"]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append(f"\nChosen model: {report['chosen_model'] or 'none; gate did not pass'}. No configuration was changed.")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    def progress(model, count, total, row):
        outcome = row["outcome"]
        if count % 10 == 0 or count == total or outcome not in {"resolved", "not_sure"}:
            print(f"ollama / {model}: {count}/{total} {outcome} ({row['elapsed_ms']:.1f} ms)", flush=True)

    try:
        report = run_benchmark(args.root, args.output, progress=progress)
    except (BenchmarkError, OSError, ValueError, yaml.YAMLError) as error:
        print(f"Stage 6 benchmark stopped: {error}")
        return 2
    print(table(report))
    print(f"Raw report: {args.output.resolve()}")
    return 0 if report["chosen_model"] is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
