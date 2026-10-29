"""Regenerate docs/COMMANDS.md, or --check its current contents."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voicebar.command_docs import render_commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = ROOT / "docs" / "COMMANDS.md"
    generated = render_commands(ROOT / "tests" / "test_parser_contract.py")
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != generated:
            print("docs/COMMANDS.md is stale; run uv run python scripts/generate_commands.py")
            return 1
        print("docs/COMMANDS.md matches the live parser grammar")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generated, encoding="utf-8", newline="\n")
    print("Generated docs/COMMANDS.md from the live parser grammar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
