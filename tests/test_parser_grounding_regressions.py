"""Transcript suffixes cannot turn into unrelated implicit app launches."""

import pytest

from voicebar.catalog import Catalog
from voicebar.parser import parse


@pytest.fixture
def catalog():
    return Catalog(
        apps=["Google Chrome", "Notepad", "Calculator", "Autodesk Fusion",
              "Property Tab Builder 2025", "Document Print Assistant"],
        aliases={"browser": "Google Chrome", "calc": "Calculator"},
        sites={"google": "https://www.google.com", "school portal": "https://school.example.edu"},
        browser_apps=("Google Chrome",),
    )


def app(name, action="open_app"):
    return {"action": action, "app": name}


@pytest.mark.parametrize("phrase", ["Open Google tab.", "open a Google tab", "open new Google tab"])
def test_site_tab_phrase_opens_only_the_configured_site(catalog, phrase):
    assert parse(phrase, catalog) == [{"action": "open_url", "url": "https://www.google.com"}]


def test_site_tab_grammar_uses_catalog_names_instead_of_google_special_case(catalog):
    assert parse("open school portal tab", catalog) == [
        {"action": "open_url", "url": "https://school.example.edu"}]
    assert parse("close school portal tab", catalog) == [
        {"action": "tab", "op": "close", "site": "school portal"}]


def test_browser_app_tab_commands_remain_explicit(catalog):
    assert parse("open a Chrome tab", catalog, active_app="Google Chrome") == [{"action": "tab", "op": "new"}]
    assert parse("open a Chrome tab", catalog, active_app="Notepad") == [
        app("Google Chrome", "switch_app"), {"action": "tab", "op": "new"}]


@pytest.mark.parametrize("phrase", [
    "open notepad tab", "open notepad print", "close notepad tab", "switch to notepad print",
    "open google something", "open google notepadd", "open notepad calculato",
    "open google unknown tab", "open school portal unknown tab", "open notepad tab unknown",
])
def test_unseparated_unknown_or_approximate_suffix_is_not_another_app(catalog, phrase):
    assert parse(phrase, catalog) is None


def test_exact_implicit_chains_and_aliases_remain_supported(catalog):
    assert parse("open notepad calculator", catalog) == [app("Notepad"), app("Calculator")]
    assert parse("open notepad calc", catalog) == [app("Notepad"), app("Calculator")]
    assert parse("close notepad calculator", catalog) == [app("Notepad", "close_app"), app("Calculator", "close_app")]


@pytest.mark.parametrize("phrase", ["open notepad and fusion", "open notepad, fusion", "open notepad. Fusion."])
def test_explicit_separator_retains_existing_subset_resolution(catalog, phrase):
    assert parse(phrase, catalog) == [app("Notepad"), app("Autodesk Fusion")]


def test_implicit_exact_target_can_be_followed_by_another_explicit_command(catalog):
    assert parse("open notepad calculator maximize it", catalog) == [
        app("Notepad"), app("Calculator"), {"action": "window", "op": "maximize", "target": "Calculator"}]


def test_explicit_app_verb_still_permits_existing_near_match(catalog):
    assert parse("open calculato", catalog) == [app("Calculator")]
    assert parse("open notepad open calculato", catalog) == [app("Notepad"), app("Calculator")]


def test_near_ties_and_exact_catalog_collisions_still_refuse(catalog):
    catalog.apps.extend(["WordPad", "WordPod"])
    assert parse("open wordpd", catalog) is None
    assert parse("open notepad and wordpd", catalog) is None
    catalog.apps.append("Calculator")
    assert parse("open notepad calculator", catalog) is None


def test_exact_app_name_ending_in_tab_is_not_reinterpreted_as_site(catalog):
    catalog.apps.append("Google Tab")
    assert parse("open Google Tab", catalog) == [app("Google Tab")]
    assert parse("open a Google Tab", catalog) == [app("Google Tab")]


def test_short_suffix_app_remains_available_when_explicitly_requested(catalog):
    assert parse("open tab", catalog) == [{"action": "tab", "op": "new"}]
    assert parse("open Property Tab Builder 2025", catalog) == [app("Property Tab Builder 2025")]


@pytest.mark.parametrize("phrase", ["do six plus seven", "ah do six plus seven", "Ah, do six plus seven."])
def test_do_arithmetic_is_scoped_to_active_calculator(catalog, phrase):
    assert parse(phrase, catalog, active_app="Calculator") == [
        {"action": "app_action", "app": "Calculator", "name": "calculate:six plus seven"}]
    assert parse(phrase, catalog, active_app="Notepad") is None


@pytest.mark.parametrize("phrase", ["do", "do six", "do six plus", "do six plus seven and nonsense"])
def test_do_requires_one_complete_calculation(catalog, phrase):
    assert parse(phrase, catalog, active_app="Calculator") is None


def test_do_arithmetic_allows_an_explicit_complete_command_after_it(catalog):
    assert parse("do six plus seven and open notepad", catalog, active_app="Calculator") == [
        {"action": "app_action", "app": "Calculator", "name": "calculate:six plus seven"}, app("Notepad")]


def test_leading_ah_does_not_rewrite_literal_payload(catalog):
    assert parse("ah open calculator", catalog) == [app("Calculator")]
    assert parse("type ah six plus seven", catalog) == [
        {"action": "type_text", "text": "ah six plus seven", "submit": False}]


@pytest.mark.parametrize("phrase", ["clear", "clear calculator", "clear the calculator",
                                   "ah clear calculator", "uh clear calculator"])
def test_clear_calculator_requires_its_current_context(catalog, phrase):
    assert parse(phrase, catalog, active_app="Calculator") == [
        {"action": "app_action", "app": "Calculator", "name": "clear"}]
    assert parse(phrase, catalog, active_app="Notepad") is None
    assert parse(phrase, catalog, last_app="Calculator") is None


def test_open_calculator_then_clear_uses_same_utterance_context(catalog):
    assert parse("open calculator and clear calculator", catalog) == [
        app("Calculator"), {"action": "app_action", "app": "Calculator", "name": "clear"}]
