# Stage 6 gate — passed

Tier 2 is enabled with **qwen2.5:3b**. The final comparison reached **54/60
exact commands (90.0%)**, **0/20 false actions**, warm resolver p50/p95
**635.8/945.9 ms**, and actual selection p50/p95 **674.8/995.5 ms**.
The full gate produced **1,684 passed in 28.78 seconds**, with no skips.
Voicebar was restarted; the selected model is loaded, and listening remains Off.

| Backend / model | Exact commands | False actions | Warm p50 | Warm p95 | Selection p50 | Selection p95 | Pick |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Ollama / qwen3:1.7b | 37/60 (61.7%) | 0/20 | 512.9 ms | 699.2 ms | 548.4 ms | 726.9 ms | Rejected |
| Ollama / qwen2.5:0.5b | 36/60 (60.0%) | 0/20 | 323.8 ms | 447.0 ms | 347.1 ms | 453.6 ms | Rejected |
| Ollama / qwen2.5:1.5b | 46/60 (76.7%) | 0/20 | 456.2 ms | 633.5 ms | 472.2 ms | 665.6 ms | Rejected |
| Ollama / gemma3:1b | 29/60 (48.3%) | 0/20 | 578.3 ms | 727.3 ms | 601.2 ms | 746.9 ms | Rejected |
| Ollama / qwen2.5:3b | 54/60 (90.0%) | 0/20 | 635.8 ms | 945.9 ms | 674.8 ms | 995.5 ms | Selected |

All five models completed 80/80 rows with zero timeouts and busy refusals.
Each model received the same frozen selector and unchanged fixture expectations.
The 20 noncommands are rejected by a conservative code guard before inference;
selection-only timings above cover the other 60 rows. This is bounded fixture
safety evidence, not a universal zero-error claim. Six command rows remain
nonexact, within the requested ≥85% acceptance threshold.

Verified:

- `uv run pytest`: 1,684 passed, including garbage-backend, raw payload,
  named-only authorization, cancellation, deadlines and teaching tests.
  The immutable parser/gate contracts and all 80 fixture expectations are unchanged.
- The 80-row draft was shown before benchmarking; user feedback supplies
  “close screen Google tab” and pointer-relative “other screen”. Remaining
  draft rows are identified as assistant-authored rather than individually approved.
- Code constructs every executable option and payload. Explicit later
  request clauses cannot be silently dropped; a named-site close cannot choose
  an unrelated unqualified active-tab close. The user's exact Google-tab phrase
  now resolves correctly in the fixture pass and a real-model post-restart probe.
- With Ollama stopped, tier-1 Notepad still produces its plan in a dry-run;
  unresolved named speech returns None and the UI mapper emits “not sure”.
  No benchmark/probe executed a native desktop action.
- The loopback/cloud-disabled server was restored with project model files.
  The selected model is retained in memory. Voicebar startup loaded speech
  before selector preload, and the deployed app remains running with listening Off.
- Ruff passes for runtime and Stage 6 scripts/tests. Full-repository Ruff still
  reports the seed's import order in the immutable gate contract; it was not changed.

Not verified:

- Live microphone-to-action timing or physical pointer-relative window movement.
- Arbitrary unseen speech or a week of real use and interactive teaching.
- ollajev, which remains uninstalled because Ollama meets the latency requirement.

The first final-config test run hit Windows clipboard contention during an
existing read-only fingerprint; its unchanged rerun passed. The earlier generated
reference edit was restored and the usage guide moved to `STAGE6-USAGE.md`.
Both failures are preserved with the previous failed model runs. No test,
threshold or race delay was changed to hide a failure.

Evidence: `docs/evidence/stage6-resolver-benchmark-final.json`,
`stage6-gate-benchmark-output.txt`, `stage6-gate-pytest-rerun.txt`,
`stage6-gate-pytest.txt`, `stage6-final-pytest.txt`,
`stage6-gate-stopped-server.json`, `stage6-fixture-review.json`, and
`stage6-runtime-*` under `docs/evidence/`. Full choices and history are in
`docs/DECISIONS.md`; operating and teaching instructions are in
`docs/STAGE6-USAGE.md`.

Stop here at the passed Stage 6 gate.
