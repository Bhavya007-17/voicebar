"""Search installed distribution Python sources for possible network entrypoints.

Hits are review leads, never a claim that every matching symbol runs at startup.
Compiled extensions are listed as an explicit static-inspection limitation.
"""

import argparse
import hashlib
import importlib.metadata
import json
import re
from pathlib import Path

PATTERNS = {
    "telemetry": re.compile(r"telemetry|sentry|posthog|analytics", re.IGNORECASE),
    "update_check": re.compile(r"update.?check|check.?for.?update|version.?check", re.IGNORECASE),
    "lazy_download": re.compile(r"snapshot_download|hf_hub_download|download_model|from_pretrained"),
    "network_entry": re.compile(r"urlopen|requests\.(?:get|post)|HTTPConnection|HTTPSConnection"),
}


def audit():
    distributions, hits = [], []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda item: item.metadata["Name"].lower()):
        files = distribution.files or []
        item = {"name": distribution.metadata["Name"], "version": distribution.version,
                "requires": distribution.requires or [], "python_files_searched": 0,
                "compiled_files": [], "hits": {key: 0 for key in PATTERNS}}
        for entry in files:
            path = Path(distribution.locate_file(entry))
            if path.suffix.lower() in {".pyd", ".dll", ".so"}:
                item["compiled_files"].append(str(entry))
            if path.suffix != ".py" or not path.is_file():
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            item["python_files_searched"] += 1
            for line_number, line in enumerate(source.splitlines(), 1):
                categories = [key for key, pattern in PATTERNS.items() if pattern.search(line)]
                if categories:
                    for category in categories:
                        item["hits"][category] += 1
                    hits.append({"distribution": item["name"], "version": item["version"],
                                 "path": str(entry), "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                                 "line": line_number, "categories": categories, "text": line.strip()[:400]})
        distributions.append(item)
    return {"distributions": distributions, "hits": hits,
            "limitations": ["Static token search produces false positives and can miss indirect calls.",
                            "Compiled extension internals were not statically audited; runtime socket proof covers the replay."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"distributions": len(report["distributions"]),
                      "python_files_searched": sum(item["python_files_searched"] for item in report["distributions"]),
                      "hits": len(report["hits"]), "output": str(args.output.resolve())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
