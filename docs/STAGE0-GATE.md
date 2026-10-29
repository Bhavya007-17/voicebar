# Stage 0 gate

Run natively on Windows, 2026-10-09, in `C:\dev\voicebar`. Stage 0 completed; stop before Stage 1.

## Verified

- `uv run voicebar --version`: exit 0, `voicebar 0.1.0`.
- `uv run pytest`: collected 0 items, no tests ran in 0.01s, exit 5. The Stage 0 prompt explicitly allows zero tests. This is not a passing test suite.
- `docs/MACHINE.md`: hardware/software inventory from saved local command/API output, including any failed or unavailable probes.
- `CLAUDE.md`: exact supplied SPEC text, with LF line endings and one final newline. `AGENTS.md` is identical for Codex.
- `.venv`: uv-managed native Windows CPython 3.12.13; pytest 9.1.1 and Ruff 0.16.10 installed as development dependencies; `uv.lock` committed.
- `uv run ruff check voicebar`: exit 0, `All checks passed!`.
- `uv run --offline --frozen voicebar --version`: exit 0, `voicebar 0.1.0`.
- Seed sanity check before bootstrap: all 267 tests passed in 0.70s. Test dependencies fetched with uv; seed was run in isolation on Python 3.12.
- Seed preservation: all 7 files have the same SHA256 as their ZIP entries; no extra files or caches in seed.
- Microphone privacy registry: machine, user, and desktop-app NonPackaged values all `Allow`; settings required no changes.
- Required ignore patterns checked against Git.

## Not verified

- Microphone recording quality or capture: no audio was recorded.
- Speech recognition, models, parser/executor integration, UI, window manipulation, performance targets, session-lock handling, and full app offline operation: later stages.
- Ollama installed model list: installed CLI's `ollama list` failed because it attempted auto-start and could not create its log under the sandbox. Exact output is in machine evidence. No service was intentionally started or models installed.
- The user has not yet confirmed the hardware inventory matches their laptop or reviewed the wake name/rules.

## Gate command output

```text
PS> uv run voicebar --version
voicebar 0.1.0

PS> uv run pytest
============================= test session starts =============================
platform win32 -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\dev\voicebar
configfile: pyproject.toml
testpaths: tests
collected 0 items

============================ no tests ran in 0.01s ============================

PS> uv run ruff check voicebar
All checks passed!

PS> uv run --offline --frozen voicebar --version
voicebar 0.1.0
```

Spec check: PASS: exact text between SPEC markers, with LF line endings and one final newline; AGENTS.md identical.

Seed check: PASS: all 7 files match ZIP SHA256; no extra files.

Raw check records: evidence/stage0-checks.json. Hardware probe commands and outputs: evidence/.
