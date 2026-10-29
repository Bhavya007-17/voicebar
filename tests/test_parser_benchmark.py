"""Measure real tier-1 parse latency over all contract command/rejection cases."""
from math import ceil
from time import perf_counter_ns

from test_parser_contract import CATALOG, COMMANDS, NOT_COMMANDS

from voicebar.parser import parse


def test_contract_parse_p95_under_five_ms():
    cases = [*COMMANDS, *((utterance, None) for utterance in NOT_COMMANDS)]
    # Warm each path, then rotate the complete set in every measured round so
    # accepted and rejected utterances receive exactly the same number of runs.
    for utterance, expected in cases:
        assert parse(utterance, CATALOG) == expected
    timings_ns = []
    rounds = 20
    for round_number in range(rounds):
        offset = round_number % len(cases)
        for utterance, expected in cases[offset:] + cases[:offset]:
            started = perf_counter_ns()
            actual = parse(utterance, CATALOG)
            timings_ns.append(perf_counter_ns() - started)
            assert actual == expected
    timings_ns.sort()
    p95_ms = timings_ns[ceil(0.95 * len(timings_ns)) - 1] / 1_000_000
    print(f"parse p95={p95_ms:.4f} ms; {len(timings_ns)} samples; "
          f"{len(COMMANDS)} accepted + {len(NOT_COMMANDS)} rejected utterances; {rounds} rounds")
    assert p95_ms < 5.0, f"parse p95 {p95_ms:.4f} ms must be below 5 ms"
