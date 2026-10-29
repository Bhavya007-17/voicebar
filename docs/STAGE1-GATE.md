# Stage 1 gate

Completed 2026-10-09 using native Windows PowerShell and uv-managed Python 3.12.13.

## Verified

- Initial unchanged seed parser contract: 141 passed in 0.39s before extensions; parser read end to end and its rules reported in ten lines.
- Final `uv run pytest`: 494 passed, including the unchanged contract, schema, catalog, CLI, grammar drift and latency checks.
- Contract p95: parse p95=0.2397 ms; 2800 samples; 117 accepted + 23 rejected utterances; 20 rounds.
- Full 223-entry catalog p95: 1.0383 ms, 1400 samples across the same 140 utterances; this measures scale rather than fixture expectations.
- Parser contract byte-identical to seed; every original seed file also compared byte-for-byte against the supplied ZIP.
- All new code lint checks passed; generated command reference matches its live grammar.
- Native StartApps refresh indexed 223 entries and retained AppIDs; all 31 spoken-alias searches found their installed targets.
- Fusion example produced the exact three expected actions.
- Cached offline planning passed for `open cloud`.
- Ten actual `voicebar plan` CLI trials all produced validated plans; no unexpected nulls. `fixtures/add-command-candidates.json` is empty.
- User chose Chrome for `browser` and the five seed site shortcuts. Pinned app list remains empty.
- Parenthesized full installed names now resolve as one app, and exact normalized-name ambiguity stops parsing before splits can select other apps.

## Not verified

- Registration is not proof an app launches successfully; no apps or windows were acted on in Stage 1.
- Audio, window/input/browser execution, recognition models, and end-of-speech latency belong to later stages.

## Contract comparison

```text
seed/tests/test_parser_contract.py  ded62ba9134934237bbb2c2e5fe2ee064503aaa7285ac61a9b1a7e43dcd943eb
tests/test_parser_contract.py       ded62ba9134934237bbb2c2e5fe2ee064503aaa7285ac61a9b1a7e43dcd943eb
Byte-identical: true
```

## Gate output

```text
PS> uv run pytest
============================= test session starts =============================
platform win32 -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\dev\voicebar
configfile: pyproject.toml
testpaths: tests
collected 494 items

tests\test_catalog.py .................................................. [ 10%]
.......................                                                  [ 14%]
tests\test_cli.py .................                                      [ 18%]
tests\test_grammar.py ......................................             [ 25%]
tests\test_parser_benchmark.py .                                         [ 26%]
tests\test_parser_contract.py .......................................... [ 34%]
........................................................................ [ 49%]
...........................                                              [ 54%]
tests\test_schema.py ................................................... [ 64%]
........................................................................ [ 79%]
........................................................................ [ 94%]
.............................                                            [100%]

============================= 494 passed in 1.84s =============================

```

```text
PS> uv run pytest -q -s tests/test_parser_benchmark.py
parse p95=0.2397 ms; 2800 samples; 117 accepted + 23 rejected utterances; 20 rounds
.
1 passed in 0.30s

```

```text
PS> uv run ruff check voicebar scripts tests/test_cli.py tests/test_catalog.py tests/test_schema.py tests/test_grammar.py tests/test_parser_benchmark.py
All checks passed!

```

```text
PS> uv run python scripts/generate_commands.py --check
docs/COMMANDS.md matches the live parser grammar

```

```text
PS> uv run voicebar plan open fusion, make it full screen, and move it to the other screen
[
  {
    "action": "open_app",
    "app": "Autodesk Fusion"
  },
  {
    "action": "window",
    "op": "maximize",
    "target": "Autodesk Fusion"
  },
  {
    "action": "window",
    "op": "move_monitor",
    "target": "Autodesk Fusion",
    "monitor": "next"
  }
]

```

```text
PS> uv run --offline --frozen voicebar plan open cloud
[
  {
    "action": "open_app",
    "app": "Claude"
  }
]

```

## Aliases added

| Spoken phrase | Installed name |
| --- | --- |
| browser | Google Chrome |
| chrome | Google Chrome |
| edge | Microsoft Edge |
| claude | Claude |
| cloud | Claude |
| fusion | Autodesk Fusion |
| fusion 360 | Autodesk Fusion |
| word | Word |
| code | Visual Studio Code |
| vs code | Visual Studio Code |
| cursor | Cursor |
| antigravity ide | Antigravity IDE |
| chatgpt | ChatGPT |
| excel | Excel |
| power point | PowerPoint |
| note pad | Notepad |
| calculator | Calculator |
| terminal | Terminal |
| explorer | File Explorer |
| file explorer | File Explorer |
| spotify | Spotify |
| whatsapp | WhatsApp |
| matlab | MATLAB R2025a |
| solidworks | SOLIDWORKS 2025 |
| bambu | Bambu Studio |
| bambu studio | Bambu Studio |
| arduino | Arduino IDE |
| teams | Microsoft Teams |
| one note | OneNote |
| paint | Paint |
| snipping tool | Snipping Tool |

Exact matching ignores spaces, case, and token-edge punctuation, so `vs code` also covers `vscode`, and `power point` also covers `powerpoint`.

## Installed names not reachable

Only the duplicate unqualified name **Antigravity** is intentionally ambiguous:

- Antigravity: `Google.Antigravity` → `null`.
- Antigravity: `electron.app.Antigravity` → `null`.

The separate Antigravity IDE entry is reachable. Every other one of the 221 Start menu records resolves to exactly one action for its full installed name.

## Ten real CLI trials

| Utterance | Actions |
| --- | --- |
| open fusion, make it full screen, and move it to the other screen | 3 |
| open cloud | 1 |
| open the browser | 1 |
| open a new word document | 1 |
| switch to code | 1 |
| open power point | 1 |
| open you tube | 1 |
| search youtube for Fusion joint tutorials | 1 |
| open notepad and type Meeting at 3 pm, bring the laptop and press enter | 2 |
| move fusion to the other monitor | 1 |

Full stdout/stderr and alias-search outputs: `docs/evidence/stage1-gate.json`. Full refreshed names/AppIDs: `docs/evidence/stage1-apps.txt`.
