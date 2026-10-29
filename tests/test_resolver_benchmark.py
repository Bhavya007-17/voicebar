"""Stage 6 provenance, local inventory, and metrics; fake models, no sockets."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
import yaml

from scripts import benchmark_stage6 as bench
from voicebar.catalog import Catalog

ACTION = {"action": "window", "op": "maximize", "target": "active"}
CATALOG = Catalog(apps=["Calculator"], sites={"google": "https://www.google.com"},
                  engines={"google": "https://www.google.com/search?q={q}"})
MODEL = "fake-local:small"


@pytest.fixture(autouse=True)
def forbid_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Benchmark tests must never open a real connection")
    monkeypatch.setattr(bench.http.client, "HTTPConnection", forbidden)


@pytest.fixture(autouse=True)
def exercise_model_metric_faults_without_request_filter(monkeypatch):
    # These tests deliberately inject false model plans and deadline failures.
    # Keep those faults observable even when the production request filter
    # would reject the synthetic repeated weather-question corpus first.
    monkeypatch.setattr("voicebar.resolver.base.eligible_request", lambda text: True)


def make_corpus(root):
    (root / "fixtures").mkdir()
    (root / "config").mkdir()
    (root / "docs" / "evidence").mkdir(parents=True)
    (root / "config" / "settings.yaml").write_text("name: computer\n", encoding="utf-8")
    rows = [{"id": f"command-{i:03}", "text": "computer make this window enormous",
             "expected": [dict(ACTION)]} for i in range(60)]
    rows += [{"id": f"noncommand-{i:03}", "text": "computer what is the weather like",
              "expected": None} for i in range(20)]
    path = root / "fixtures" / "loose.yaml"
    path.write_text(yaml.safe_dump(rows), encoding="utf-8")
    write_receipt(root)
    return rows


def test_real_request_filter_is_reported_separately_from_model_abstention(corpus, monkeypatch):
    from voicebar.resolver.eligibility import eligible_request

    monkeypatch.setattr("voicebar.resolver.base.eligible_request", eligible_request)
    root, _ = corpus
    backend = FakeBackend(correct=60, false_actions=20)
    report = bench.run_benchmark(root, root / "report.json", request=FakeInventory(),
                                 backend_factory=lambda name: backend, timer=timer_for([10] * 80))
    model = report["models"][0]
    assert backend.command_calls == 60 and backend.noncommand_calls == 0
    assert model["guarded_noncommands"] == 20 and model["selection_rows"] == 60
    assert model["false_actions"] == 0 and model["completed_rows"] == 80
    assert all(row["outcome"] == "non_request" for row in model["rows"][60:])


def write_receipt(root, **overrides):
    raw = (root / "fixtures" / "loose.yaml").read_bytes()
    receipt = {"shown": True, "feedback_received": True,
               "fixture_sha256": hashlib.sha256(raw).hexdigest(), **overrides}
    path = root / "docs" / "evidence" / "stage6-fixture-review.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def metadata(count=1_000_000_000, **overrides):
    return {"model_info": {"general.parameter_count": count},
            "capabilities": ["completion"], **overrides}


class FakeInventory:
    def __init__(self, infos=None, entries=None):
        self.infos = {MODEL: metadata()} if infos is None else infos
        self.entries = [{"name": name, "digest": "fake-digest"} for name in self.infos] if entries is None else entries
        self.calls = []

    def __call__(self, path, body=None, *, timeout_s):
        self.calls.append((path, body, timeout_s))
        if path == "/api/tags":
            return {"models": copy.deepcopy(self.entries)}
        assert path == "/api/show"
        return copy.deepcopy(self.infos[body["model"]])


class FakeBackend:
    def __init__(self, *, correct=60, false_actions=0, warm=True):
        self.correct, self.false_actions, self.warmed = correct, false_actions, warm
        self.command_calls = self.noncommand_calls = 0
        self.warm_calls, self.budgets = [], []

    def warm(self, timeout_s):
        self.warm_calls.append(timeout_s)
        return self.warmed

    def select(self, request, timeout_s):
        self.budgets.append(timeout_s)
        if request.text == "make this window enormous":
            self.command_calls += 1
            choose = self.command_calls <= self.correct
        else:
            assert request.text == "what is the weather like"
            self.noncommand_calls += 1
            choose = self.noncommand_calls <= self.false_actions
        if choose:
            identifier = next(candidate.id for candidate in request.candidates if candidate.action == ACTION)
            return json.dumps({"actions": [identifier]})
        return '{"actions":["none"]}'


def timer_for(milliseconds):
    values = []
    for index, elapsed in enumerate(milliseconds):
        values.extend([index * 10, index * 10 + elapsed / 1000])
    iterator = iter(values)
    return lambda: next(iterator)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    rows = make_corpus(tmp_path)
    monkeypatch.setattr(bench, "load_catalog", lambda root: SimpleNamespace(catalog=CATALOG))
    return tmp_path, rows


@pytest.mark.parametrize("problem", ["missing", "not_shown", "no_feedback", "wrong_hash", "changed_fixture", "malformed"])
def test_review_gate_precedes_inventory_warming_and_inference(corpus, problem):
    root, _ = corpus
    receipt = root / "docs" / "evidence" / "stage6-fixture-review.json"
    if problem == "missing":
        receipt.unlink()
    elif problem == "not_shown":
        write_receipt(root, shown=False)
    elif problem == "no_feedback":
        write_receipt(root, feedback_received=False)
    elif problem == "wrong_hash":
        write_receipt(root, fixture_sha256="0" * 64)
    elif problem == "changed_fixture":
        path = root / "fixtures" / "loose.yaml"
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
    else:
        receipt.write_text("{", encoding="utf-8")
    inventory = FakeInventory()
    created = []
    with pytest.raises(bench.BenchmarkError, match="review"):
        bench.run_benchmark(root, root / "report.json", request=inventory,
                            backend_factory=lambda name: created.append(name))
    assert inventory.calls == [] and created == []
    assert not (root / "report.json").exists()


def test_inventory_filters_numeric_size_capability_and_remote_metadata():
    infos = {
        "fake-boundary:small": metadata(4_000_000_000),
        "fake-large:small": metadata(4_000_000_001),
        "fake-embedding:small": metadata(capabilities=["embedding"]),
        "fake-bool:small": metadata(True),
        "fake-string:small": metadata("3B"),
        "fake-missing:small": metadata(model_info={}),
        "fake-remote:small": metadata(remote_host="https://remote.invalid"),
        "fake-forwarded:small": metadata(remote_model="remote"),
        "fake-cloud:small": metadata(),
    }
    inventory = FakeInventory(infos)
    models, excluded = bench.discover_models(inventory)
    assert [model["model"] for model in models] == ["fake-boundary:small"]
    assert len(excluded) == 8
    assert inventory.calls[0][0] == "/api/tags"
    assert all(call[0] == "/api/show" for call in inventory.calls[1:])
    assert "fake-cloud:small" not in [call[1]["model"] for call in inventory.calls[1:]]


def test_remote_tags_and_duplicates_cannot_infer_or_duplicate_a_pass():
    inventory = FakeInventory(entries=[{"name": MODEL}, {"name": MODEL},
                                      {"name": "fake-proxy:small", "remote_model": "remote"}])
    models, excluded = bench.discover_models(inventory)
    assert len(models) == len(excluded) == 1
    assert len(inventory.calls) == 2


@pytest.mark.parametrize("bad", [{}, {"models": {}}, {"models": [None]}, {"models": [{"name": "bad name"}]}])
def test_malformed_inventory_stops_closed(bad):
    with pytest.raises(bench.BenchmarkError):
        bench.discover_models(lambda *args, **kwargs: bad)


@pytest.mark.parametrize("problem", ["count", "duplicate", "unnamed", "tier_one", "unknown_app", "invalid_plan", "payload"])
def test_fixture_validation_rejects_invalid_contract_rows(corpus, problem):
    root, rows = corpus
    if problem == "count":
        rows.pop()
    elif problem == "duplicate":
        rows[1]["id"] = rows[0]["id"]
    elif problem == "unnamed":
        rows[0]["text"] = "make this window enormous"
    elif problem == "tier_one":
        rows[0]["text"] = "computer open calculator"
    elif problem == "unknown_app":
        rows[0]["expected"] = [{"action": "open_app", "app": "Absent app"}]
    elif problem == "invalid_plan":
        rows[0]["expected"] = [{**ACTION, "extra": True}]
    else:
        rows[0]["expected"] = [{"action": "type_text", "text": "invented text", "submit": False}]
    path = root / "fixtures" / "loose.yaml"
    path.write_text(yaml.safe_dump(rows), encoding="utf-8")
    write_receipt(root)
    inventory = FakeInventory()
    with pytest.raises(bench.BenchmarkError, match="fixtures"):
        bench.run_benchmark(root, root / "report.json", request=inventory)
    assert inventory.calls == []


def test_one_warm_pass_exact_plans_latency_and_raw_provenance(corpus):
    root, rows = corpus
    settings = (root / "config" / "settings.yaml").read_bytes()
    raw = (root / "fixtures" / "loose.yaml").read_bytes()
    backend = FakeBackend(correct=51)
    created = []

    def factory(name):
        created.append(name)
        return backend

    output = root / "report.json"
    report = bench.run_benchmark(root, output, request=FakeInventory(), backend_factory=factory,
                                 timer=timer_for([10] * 40 + [20] * 40))
    assert created == [MODEL] and backend.warm_calls == [60.0]
    assert backend.command_calls == 60 and backend.noncommand_calls == 20
    assert all(0 < budget <= 1.5 for budget in backend.budgets)
    model = report["models"][0]
    assert model["accuracy"] == .85 and model["false_actions"] == 0 and model["eligible"]
    assert model["p50_ms"] == pytest.approx(10) and model["p95_ms"] == pytest.approx(20)
    assert len(model["rows"]) == 80 and model["rows"][0]["plan"] == [ACTION]
    assert model["rows"][0]["expected"] == rows[0]["expected"]
    assert model["unresolved"] == 9 and report["chosen_model"] == MODEL
    assert report["fixture_sha256"] == hashlib.sha256(raw).hexdigest()
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert (root / "config" / "settings.yaml").read_bytes() == settings
    assert (root / "fixtures" / "loose.yaml").read_bytes() == raw


@pytest.mark.parametrize("correct,false_actions,elapsed", [(50, 0, 10), (60, 1, 10), (60, 0, 1501)])
def test_accuracy_false_actions_and_latency_each_enforce_pick_gate(corpus, correct, false_actions, elapsed):
    root, _ = corpus
    backend = FakeBackend(correct=correct, false_actions=false_actions)
    report = bench.run_benchmark(root, root / "report.json", request=FakeInventory(),
                                 backend_factory=lambda name: backend, timer=timer_for([elapsed] * 80))
    assert report["chosen_model"] is None and not report["models"][0]["eligible"]
    assert len(backend.warm_calls) == 1 and len(backend.budgets) == 80


def test_failed_warm_is_recorded_once_and_never_infers(corpus):
    root, _ = corpus
    backend = FakeBackend(warm=False)
    report = bench.run_benchmark(root, root / "report.json", request=FakeInventory(),
                                 backend_factory=lambda name: backend)
    assert report["chosen_model"] is None and report["models"][0]["status"] == "warm_failed"
    assert backend.warm_calls == [60.0] and backend.budgets == []


def test_garbage_backend_counts_as_unresolved_without_raising(corpus):
    root, _ = corpus
    backend = FakeBackend()
    backend.select = lambda request, timeout_s: "not JSON"
    report = bench.run_benchmark(root, root / "report.json", request=FakeInventory(),
                                 backend_factory=lambda name: backend, timer=timer_for([10] * 80))
    assert report["chosen_model"] is None
    assert report["models"][0]["unresolved"] == 60 and report["models"][0]["false_actions"] == 0


def test_every_eligible_model_is_benchmarked_once_before_pick(corpus):
    root, _ = corpus
    infos = {MODEL: metadata(), "fake-second:small": metadata(2_000_000_000)}
    backends = {MODEL: FakeBackend(correct=55), "fake-second:small": FakeBackend(correct=60)}
    report = bench.run_benchmark(root, root / "report.json", request=FakeInventory(infos),
                                 backend_factory=backends.__getitem__, timer=timer_for([10] * 160))
    assert report["chosen_model"] == "fake-second:small"
    assert all(backend.warm_calls == [60.0] and len(backend.budgets) == 80 for backend in backends.values())


def test_existing_evidence_is_preserved_without_rebenchmark(corpus):
    root, _ = corpus
    output = root / "report.json"
    output.write_text("original evidence", encoding="utf-8")
    inventory = FakeInventory()
    with pytest.raises(bench.BenchmarkError, match="already exists"):
        bench.run_benchmark(root, output, request=inventory)
    assert inventory.calls == [] and output.read_text(encoding="utf-8") == "original evidence"


@pytest.mark.parametrize("chosen,expected_exit", [(MODEL, 0), (None, 2)])
def test_cli_outputs_table_and_gate_exit_status(monkeypatch, capsys, chosen, expected_exit):
    report = {"models": [{"model": MODEL, "status": "complete", "exact_commands": 60,
                          "accuracy": 1, "false_actions": 0, "p50_ms": 10,
                          "p95_ms": 20, "eligible": chosen is not None}], "chosen_model": chosen}
    monkeypatch.setattr(bench, "run_benchmark", lambda *args, **kwargs: report)
    assert bench.main(["--output", "fake-report.json"]) == expected_exit
    output = capsys.readouterr().out
    assert "60/60 (100.0%)" in output and "0/20" in output and "20.0 ms" in output


def test_inventory_transport_only_connects_to_literal_loopback(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def request(self, method, path, body, headers):
            calls.append((method, path, body))

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda limit: b'{"models":[]}')

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(bench.http.client, "HTTPConnection", Connection)
    assert bench._local_request("/api/tags") == {"models": []}
    assert calls == [("127.0.0.1", 11434, 10.0), ("GET", "/api/tags", None), "closed"]


def test_timeout_waits_for_actual_worker_before_next_fixture(corpus, monkeypatch):
    root, _ = corpus
    release = threading.Event()
    backend = FakeBackend()
    select = backend.select
    calls = []

    def delayed_first(request, timeout_s):
        calls.append(request.text)
        if len(calls) == 1:
            release.wait()
        return select(request, timeout_s)

    backend.select = delayed_first
    original_resolver = bench.Resolver

    class ObservedResolver(original_resolver):
        def wait_idle(self, timeout_s):
            if self.last_outcome == "timeout":
                assert not super().wait_idle(0)
                release.set()
            return super().wait_idle(timeout_s)

    monkeypatch.setattr(bench, "Resolver", ObservedResolver)
    monkeypatch.setattr(bench, "TIMEOUT_MS", 20)
    try:
        report = bench.run_benchmark(root, root / "report.json", request=FakeInventory(),
                                     backend_factory=lambda name: backend,
                                     timer=timer_for([21] + [10] * 79))
    finally:
        release.set()
    model = report["models"][0]
    assert len(calls) == 80 and len(backend.budgets) == 80
    assert model["rows"][0]["outcome"] == "timeout"
    assert model["rows"][0]["plan"] is None and model["rows"][0]["worker_completed"]
    assert all(row["outcome"] != "busy" for row in model["rows"])
    assert model["timeout_count"] == 1 and model["busy_count"] == 0
    assert model["completed_rows"] == 80 and model["coverage"] == 1
    assert model["status"] == "complete" and model["accuracy"] == 59 / 60
    assert model["p95_ms"] == pytest.approx(10)


def test_hung_transport_aborts_model_as_incomplete_instead_of_79_busy_results(corpus, monkeypatch):
    root, _ = corpus
    release = threading.Event()
    entered = threading.Event()
    worker = []
    backend = FakeBackend()

    def hanging(request, timeout_s):
        worker.append(threading.current_thread())
        entered.set()
        release.wait()
        return '{"actions":["none"]}'

    backend.select = hanging
    monkeypatch.setattr(bench, "TIMEOUT_MS", 20)
    monkeypatch.setattr(bench, "WORKER_TEARDOWN_TIMEOUT_S", .02)
    try:
        report = bench.run_benchmark(root, root / "report.json", request=FakeInventory(),
                                     backend_factory=lambda name: backend)
        assert entered.is_set()
        model = report["models"][0]
        assert model["status"] == "incomplete" and not model["eligible"]
        assert model["attempted_rows"] == 1 and model["completed_rows"] == 0
        assert model["coverage"] == 0 and model["timeout_count"] == 1
        assert model["busy_count"] == 0 and len(model["rows"]) == 1
        assert not model["rows"][0]["worker_completed"]
        assert model["measured_noncommands"] == 0 and report["chosen_model"] is None
        assert "0/80" in bench.table(report) and "incomplete" in bench.table(report)
    finally:
        release.set()
        for thread in worker:
            thread.join(timeout=1)


def test_busy_attempt_is_not_counted_as_an_evaluated_fixture(corpus, monkeypatch):
    root, _ = corpus

    class BusyResolver:
        def __init__(self, *args, **kwargs):
            self.last_outcome = "not_started"

        def resolve(self, *args, **kwargs):
            self.last_outcome = "busy"

        def wait_idle(self, timeout_s):
            return True

    monkeypatch.setattr(bench, "Resolver", BusyResolver)
    backend = FakeBackend()
    report = bench.run_benchmark(root, root / "report.json", request=FakeInventory(),
                                 backend_factory=lambda name: backend)
    model = report["models"][0]
    assert model["status"] == "incomplete" and not model["eligible"]
    assert model["coverage"] == 0 and model["busy_count"] == 1
    assert model["attempted_rows"] == 1 and backend.budgets == []
    assert report["chosen_model"] is None


def test_cli_progress_prints_periodic_rows_and_each_abnormal_outcome(monkeypatch, capsys):
    def run(*args, progress):
        for count, outcome in [(1, "resolved"), (10, "resolved"), (11, "timeout"),
                               (12, "busy"), (13, "invalid_output"), (80, "not_sure")]:
            progress(MODEL, count, 80, {"outcome": outcome, "elapsed_ms": 20})
        return {"models": [], "chosen_model": None}

    monkeypatch.setattr(bench, "run_benchmark", run)
    assert bench.main(["--output", "fake-report.json"]) == 2
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("ollama /")]
    assert len(lines) == 5
    assert "10/80 resolved" in lines[0] and "11/80 timeout" in lines[1]
    assert "12/80 busy" in lines[2] and "13/80 invalid_output" in lines[3]
    assert "80/80 not_sure" in lines[4]
