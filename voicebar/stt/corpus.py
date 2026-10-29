"""Model-independent contract corpus preparation; this module never executes plans.

Synthetic speech is diagnostic material, not evidence for selecting a backend.
The empty rejected utterance is represented by 100 ms of PCM silence because
System.Speech cannot Speak an empty string.
"""

from __future__ import annotations

import ast
import hashlib
import json
import wave
from dataclasses import dataclass
from pathlib import Path

from ..catalog import Catalog
from ..normalization import normalized_words
from ..parser import parse, resolve
from ..schema import validate_plan

SAMPLE_RATE = 16000
SYNTHETIC_RATES = (0, 2)
SYNTHETIC_VOICE = "Microsoft David Desktop"
_BUILDERS = {
    "app", "switch", "close", "win", "url", "search", "tab", "nav", "typed",
    "press", "scroll", "volume", "media", "mode",
}
_DATA = {"CATALOG", "CHROME", "FUSION", "YOUTUBE", "COMMANDS", "NOT_COMMANDS"}
_NODES = (
    ast.Module, ast.Assign, ast.Name, ast.Store, ast.Load, ast.Constant, ast.List,
    ast.Tuple, ast.Dict, ast.Call, ast.keyword, ast.FunctionDef, ast.arguments,
    ast.arg, ast.Return, ast.IfExp, ast.Compare, ast.Is,
)


class CorpusError(ValueError):
    """Corpus preparation failed before recognition or execution."""


@dataclass(frozen=True)
class ContractCase:
    id: str
    text: str
    reference_plan: list[dict] | None
    kind: str
    index: int
    reference_last_app: str | None = None


@dataclass(frozen=True)
class ContractCorpus:
    cases: tuple[ContractCase, ...]
    sha256: str
    path: Path
    catalog: Catalog


def _assignment_names(node: ast.Assign) -> set[str]:
    names = set()
    for target in node.targets:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Tuple) and all(isinstance(part, ast.Name) for part in target.elts):
            names.update(part.id for part in target.elts)
        else:
            raise CorpusError("Contract data has an unsupported assignment target.")
    return names


def _validate_projection(node: ast.AST, names: set[str]) -> None:
    """Only literal containers, named data, and the contract's pure builders."""
    for part in ast.walk(node):
        if not isinstance(part, _NODES):
            raise CorpusError(f"Unsupported contract expression: {type(part).__name__}.")
        if isinstance(part, ast.Name) and part.id not in names:
            raise CorpusError(f"Unsupported contract name: {part.id}.")
        if isinstance(part, ast.Call) and (
            not isinstance(part.func, ast.Name) or part.func.id not in _BUILDERS | {"Catalog"}
        ):
            raise CorpusError("Contract data may call only Catalog and pure action builders.")
        if isinstance(part, ast.FunctionDef):
            if part.decorator_list or part.returns or part.type_comment or part.type_params:
                raise CorpusError("Contract action builders must be plain functions.")
            if any(isinstance(child, ast.Call) for child in ast.walk(part)):
                raise CorpusError("Contract action builders must not call functions.")


def _evaluate(node: ast.AST, namespace: dict):
    _validate_projection(node, set(namespace) - {"__builtins__"})
    return eval(compile(ast.Expression(node), "<contract expression>", "eval"), namespace)


def load_contract(path: Path) -> ContractCorpus:
    """Read data and four context assertions without importing pytest/test modules.

    This is intentionally a restricted projection, rather than executing the
    test file. Imports, attributes, decorators, loops and arbitrary calls are
    refused in projected nodes. A changed contract grammar fails visibly.
    """
    path = Path(path)
    raw = path.read_bytes()
    try:
        module = ast.parse(raw.decode("utf-8-sig"), filename=str(path))
    except (SyntaxError, UnicodeError) as error:
        raise CorpusError(f"Cannot read parser contract: {error}") from error
    projected = []
    seen_data = set()
    seen_builders = set()
    local_names = set()
    for node in module.body:
        if isinstance(node, ast.Assign):
            assigned = _assignment_names(node)
            if assigned & _DATA:
                if assigned - _DATA or assigned & seen_data:
                    raise CorpusError("Contract data names must be unique.")
                seen_data.update(assigned)
                projected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in _BUILDERS:
            if node.name in seen_builders:
                raise CorpusError("Contract action builder names must be unique.")
            seen_builders.add(node.name)
            local_names.update(part.arg for part in ast.walk(node) if isinstance(part, ast.arg))
            local_names.update(part.id for part in ast.walk(node) if isinstance(part, ast.Name))
            projected.append(node)
    if seen_data != _DATA or seen_builders != _BUILDERS:
        raise CorpusError("Parser contract is missing required data or action builders.")
    projection = ast.Module(body=projected, type_ignores=[])
    _validate_projection(projection, _DATA | _BUILDERS | local_names | {"Catalog"})
    namespace = {"Catalog": Catalog, "__builtins__": {}}
    # _validate_projection enforces the literal/container node allowlist and
    # pure builders above. Empty builtins add a second boundary; neither the
    # test module's imports nor its top-level/test code are included here.
    exec(compile(projection, str(path), "exec"), namespace)  # noqa: S102
    reference_catalog = namespace["CATALOG"]
    if not isinstance(reference_catalog, Catalog):
        raise CorpusError("Contract CATALOG must be a Catalog.")
    cases = []
    for index, row in enumerate(namespace["COMMANDS"], 1):
        if not isinstance(row, tuple) or len(row) != 2 or not isinstance(row[0], str):
            raise CorpusError("Contract COMMANDS must contain (text, plan) pairs.")
        text, plan = row
        validate_plan(plan)
        if parse(text, reference_catalog) != plan:
            raise CorpusError(f"Contract expectation does not match parser: {text!r}.")
        cases.append(ContractCase(f"command-{index:03d}", text, plan, "command", index))
    for index, text in enumerate(namespace["NOT_COMMANDS"], 1):
        if not isinstance(text, str) or parse(text, reference_catalog) is not None:
            raise CorpusError(f"Contract rejection does not match parser: {text!r}.")
        cases.append(ContractCase(f"rejected-{index:03d}", text, None, "rejected", index))
    # Inspect the previous-utterance expectations as data; never invoke tests.
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        for assertion in node.body:
            if not isinstance(assertion, ast.Assert):
                continue
            comparison = assertion.test
            if not isinstance(comparison, ast.Compare) or len(comparison.ops) != 1:
                continue
            call = comparison.left
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "parse":
                continue
            context = [keyword for keyword in call.keywords if keyword.arg == "last_app"]
            if not context:
                continue
            if (len(call.args) != 2 or len(call.keywords) != 1
                    or not isinstance(comparison.ops[0], ast.Eq)
                    or not isinstance(call.args[1], ast.Name) or call.args[1].id != "CATALOG"):
                raise CorpusError("Unsupported contextual parser assertion.")
            text = _evaluate(call.args[0], namespace)
            last_app = _evaluate(context[0].value, namespace)
            plan = _evaluate(comparison.comparators[0], namespace)
            if not isinstance(text, str) or not isinstance(last_app, str):
                raise CorpusError("Contextual assertion text and last_app must be strings.")
            validate_plan(plan)
            if parse(text, reference_catalog, last_app=last_app) != plan:
                raise CorpusError(f"Contextual contract expectation does not match parser: {text!r}.")
            index = sum(case.kind == "context" for case in cases) + 1
            cases.append(ContractCase(f"context-{index:03d}", text, plan, "context", index, last_app))
    if not any(case.kind == "context" for case in cases):
        raise CorpusError("Parser contract has no previous-utterance expectations.")
    return ContractCorpus(tuple(cases), hashlib.sha256(raw).hexdigest(), path, reference_catalog)


def build_synthetic_manifest(project_root: Path, actual_catalog: Catalog) -> dict:
    """Describe every contract evaluation at both fixed speech rates.

    WAV paths are relative to the manifest's directory. Original expectations
    remain separate from the result of parsing against this machine's catalog.
    A missing contextual app has no meaningful installed-catalog expectation.
    """
    root = Path(project_root)
    contract = load_contract(root / "tests" / "test_parser_contract.py")
    items = []
    for case in contract.cases:
        last_app = None
        context_available = True
        if case.reference_last_app is not None:
            match = resolve(normalized_words(case.reference_last_app), actual_catalog)
            context_available = match is not None and match[0] == "app"
            last_app = match[1] if context_available else None
        expected = parse(case.text, actual_catalog, last_app=last_app) if context_available else None
        if expected is not None:
            validate_plan(expected)
        for rate in SYNTHETIC_RATES:
            identifier = f"{case.id}-rate{rate}"
            items.append({
                "id": identifier,
                "text": case.text,
                "wav": f"{identifier}.wav",
                "expected_plan": expected,
                "reference_plan": case.reference_plan,
                "rate": rate,
                "source": {
                    "file": "tests/test_parser_contract.py",
                    "kind": case.kind,
                    "index": case.index,
                    "case_id": case.id,
                },
                "contract_sha256": contract.sha256,
                "last_app": last_app,
                "reference_last_app": case.reference_last_app,
                "context_available": context_available,
                "audio_kind": "silence" if not case.text else "system_speech",
            })
    catalog_snapshot = {
        "apps": list(actual_catalog.apps), "aliases": dict(actual_catalog.aliases),
        "sites": dict(actual_catalog.sites), "engines": dict(actual_catalog.engines),
    }
    return {
        "version": 1,
        "purpose": "synthetic diagnostics; backend selection requires personal recordings",
        "voice": SYNTHETIC_VOICE,
        "rates": list(SYNTHETIC_RATES),
        "format": {"sample_rate": SAMPLE_RATE, "channels": 1, "sample_width": 2, "encoding": "PCM"},
        "contract_sha256": contract.sha256,
        "catalog": catalog_snapshot,
        "items": items,
    }


def validate_wav(path: Path) -> dict:
    """Require nonempty 16 kHz mono PCM16; integer PCM samples are finite."""
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (1, SAMPLE_RATE, 2, "NONE"):
                raise CorpusError(f"{Path(path).name}: WAV must be 16 kHz mono PCM16.")
            frames = wav.getnframes()
            data = wav.readframes(frames)
            if not frames or len(data) != frames * 2:
                raise CorpusError(f"{Path(path).name}: WAV is empty or truncated.")
    except (OSError, wave.Error, EOFError) as error:
        raise CorpusError(f"Cannot read WAV {path}: {error}") from error
    return {
        "frames": frames,
        "duration_seconds": frames / SAMPLE_RATE,
        "wav_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
    }


def write_empty_utterance(path: Path) -> None:
    """Preserve the empty rejection case as exactly 100 ms of PCM silence."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"\0\0" * (SAMPLE_RATE // 10))


def save_manifest(path: Path, manifest: dict) -> None:
    path = Path(path)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
