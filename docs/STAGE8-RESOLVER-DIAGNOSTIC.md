# Resolver deadline investigation — 2026-10-23

The second full Stage 8 pytest run retained in
`evidence/stage8-gate-pytest-final.txt` reported one failure, 2,132 passes and
one existing skip. The failing existing test was
`test_actual_worker_completion_is_observable_without_reviving_late_plan`:
after observing completion of a deliberately timed-out worker, its second
call returned `None` instead of the selected plan under the unchanged **20 ms**
deadline. That failure remains evidence; this diagnostic does not replace it.

## Verified

The exact unchanged test passed by itself: `1 passed in 0.32s`. An isolated
100-iteration fake-backend diagnostic, using the same timeout/release/second-call
sequence, resolved all 100 second calls. It opened no devices or sockets and
loaded no model. The raw report is
`evidence/stage8-resolver-deadline-diagnostic.json`.

| Observed second-call phase | p50 | p95 | maximum |
| --- | ---: | ---: | ---: |
| Worker thread start delay | 0.509 ms | 5.256 ms | 14.015 ms |
| Worker wall time | 2.019 ms | 7.673 ms | 12.909 ms |
| Complete resolve call, measured with performance counter | 2.866 ms | 12.100 ms | 23.952 ms |

Installed-clock inspection separately reported `time.monotonic()` using
`GetTickCount64()` with **15.625 ms** resolution, while `time.perf_counter()`
uses `QueryPerformanceCounter()` with 0.0001 ms resolution. Resolver deadlines
use the former. This explains how a call can measure over 20 ms with the
performance counter yet complete inside the resolver's quantized deadline.

The original diagnostic's CPU summary is unavailable: its observer treated a
zero thread-CPU sample as missing. That observer was corrected for future use;
the saved 100-iteration report was not rerun or rewritten as a new measurement.

## Not verified

The full-run failure did not retain its second-call outcome or phase timings.
Scheduler contention and clock-boundary alignment are supported explanations,
not a proven reconstruction of that specific failure. Read-only review and
the isolated diagnostic did not identify a worker lifecycle error. No product
source, expectation, deadline, threshold or sleep was changed in response to
this investigation. A later full gate must be reported independently.
