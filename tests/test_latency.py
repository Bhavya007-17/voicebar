"""Log percentiles retain missing-hop information and never use action duration."""

import json

from voicebar.latency import markdown_table, percentiles, summarize_logs


def test_percentiles_use_nearest_rank_and_reject_unmeasured_values():
    result = percentiles([1, 2, 3, 4, 100, None, True, -1, float("nan")])
    assert result == {"count": 5, "p50_ms": 3, "p95_ms": 100}


def test_json_hops_do_not_substitute_completion_for_execution_start(tmp_path):
    path = tmp_path / "decisions.jsonl"
    row = {"event": "decision", "timings": {"endpoint_ms": 256, "stt_ms": 900,
                                             "gate_ms": 1, "execution_ms": 6000},
           "results": [{"milliseconds": 6000, "started_ms": 4}]}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = summarize_logs([path])
    assert result["hops"]["recognition"]["p50_ms"] == 900
    assert result["hops"]["execute_start"]["count"] == 0
    assert "unavailable" in markdown_table(result)


def test_corrupt_lines_are_retained_as_errors_without_losing_other_rows(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('bad\n{"event":"state"}\n{"event":"decision","timings":{"gate_ms":1}}\n',
                    encoding="utf-8")
    result = summarize_logs([path, tmp_path / "missing.jsonl"])
    assert result["decision_rows"] == 1
    assert len(result["errors"]) == 2
    assert result["errors"][0]["line"] == 1
