"""ASR context has live grammar provenance and no fixture/answer dependency."""

import builtins
import re
import subprocess
import sys
from pathlib import Path

import pytest

from voicebar.catalog import Catalog
from voicebar.stt import context


def vocabulary(prompt):
    return prompt.split("\nVocabulary: ", 1)[1].removesuffix(".").split(", ")


def test_context_contains_complete_spoken_vocabulary_and_catalog_engines():
    catalog = Catalog(apps=["Claude", "Autodesk Fusion"], aliases={"cloud": "Claude"},
                      sites={"youtube": "https://youtube.com"}, engines={"brave": "https://search.brave.com/?q={query}"})
    prompt = context.build_context(catalog)
    terms = set(vocabulary(prompt))
    assert prompt.startswith(context.DOMAIN_CONTEXT + "\nVocabulary: ")
    assert {"Claude", "Autodesk Fusion", "cloud", "youtube", "brave", "google",
            "write", "type", "dictate", "pause", "play", "resume", "right",
            "left", "maximize", "restore", "reopen", "search", "control", "escape"} <= terms
    assert not {"dictation_on", "dictation_off", "play_pause", "snap_left", "action", "op", "head", "mon"} & terms
    assert "https://" not in prompt and "{query}" not in prompt


def test_regex_extraction_drops_names_flags_comments_escapes_and_classes():
    pattern = (r"^(?P<private_label>\w+) (?:press|hit) [a-zA-Z0-9] []a-z] "
               r"\x41 \u0042 \U00000043 \N{SPACE} (?P=private_label) "
               r"(?# hidden_comment)(?i:write)\b$")
    assert context.literal_words(pattern) == ("press", "hit", "write")
    assert context.literal_words(r"(?<=left )right(?= tab)") == ("left", "right", "tab")


@pytest.mark.parametrize("pattern", ["bad\\", "[a-z", "(?P<unfinished", "(?P=unfinished", r"\N{unfinished"])
def test_unfinished_regex_metadata_fails_instead_of_leaking_into_context(pattern):
    with pytest.raises(ValueError, match="Grammar context"):
        context.literal_words(pattern)


def test_live_grammar_change_changes_context_without_editing_any_fixture(monkeypatch):
    catalog = Catalog()
    original = context.build_context(catalog)
    monkeypatch.setattr(context.grammar, "TYPE_VERBS", (*context.grammar.TYPE_VERBS, "frobnicate"))
    changed = context.build_context(catalog)
    assert original != changed and "frobnicate" in vocabulary(changed)


def test_pattern_labels_and_action_metadata_never_enter_context(monkeypatch):
    monkeypatch.setattr(context.grammar, "FIXED", [
        (r"frobnicate now", {"secretmetadata": "expectedpayload", "action": "secret_action"}),
    ])
    monkeypatch.setattr(context.grammar, "CLAUSE_PATTERNS", {
        "privatepatternlabel": re.compile(r"^(?P<privategroup>.+) wobble$"),
    })
    terms = set(vocabulary(context.build_context(Catalog())))
    assert {"frobnicate", "wobble"} <= terms
    assert not {"secretmetadata", "expectedpayload", "action", "secret_action", "privatepatternlabel", "privategroup"} & terms


def test_unallowlisted_module_data_cannot_add_an_answer(monkeypatch):
    original = context.build_context(Catalog())
    monkeypatch.setattr(context.grammar, "UNRELATED_EXPECTED_TRANSCRIPT", "dear professor meeting at 3 pm", raising=False)
    assert context.build_context(Catalog()) == original


def test_context_order_is_stable_across_catalog_insertion_order():
    first = Catalog(apps=["Claude", "Notepad"], aliases={"cloud": "Claude", "notes": "Notepad"},
                    sites={"youtube": "https://youtube.com", "github": "https://github.com"},
                    engines={"brave": "https://brave.com", "bing": "https://bing.com"})
    reordered = Catalog(apps=list(reversed(first.apps)), aliases=dict(reversed(list(first.aliases.items()))),
                        sites=dict(reversed(list(first.sites.items()))), engines=dict(reversed(list(first.engines.items()))))
    prompt = context.build_context(first)
    assert prompt == context.build_context(reordered)
    terms = vocabulary(prompt)
    assert terms == sorted(terms, key=lambda value: (value.casefold(), value))
    assert len(terms) == len({value.casefold() for value in terms})


def test_context_builder_performs_no_file_reads(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Context must not read fixtures or any other files.")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert "Claude" in context.build_context(Catalog(apps=["Claude"]))


def test_import_and_build_do_not_import_contract_or_recording_helpers():
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib.abc
import sys
class RefuseFixtures(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith(('tests', 'test_')) or fullname in {
            'voicebar.command_docs', 'voicebar.recording', 'voicebar.stt.corpus', 'voicebar.stt.benchmark'
        }):
            raise RuntimeError('ASR context imported fixture/helper module: ' + fullname)
sys.meta_path.insert(0, RefuseFixtures())
from voicebar.catalog import Catalog
from voicebar.stt.context import build_context
assert 'Claude' in build_context(Catalog(apps=['Claude']))
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=root,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
