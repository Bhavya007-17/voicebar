# Voicebar seed

Reference implementations and their contract tests. Stage 1 and Stage 4 of the
build pack copy these into the project.

    voicebar/catalog.py              Catalog dataclass (apps, aliases, sites, engines)
    voicebar/parser.py               tier-1 parser: text -> list of action dicts, or None
    voicebar/gate.py                 gate: name, follow-up window, open mode, sleep, dictation
    tests/test_parser_contract.py    141 tests
    tests/test_gate_contract.py      126 tests

Check it before using it (needs rapidfuzz and pytest), from this folder:

    python -m pytest -q        ->  267 passed

The tests are the spec. Do not edit an expectation to make it pass.
