"""Catalog validation and the Windows enumeration boundary, without launching apps."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicebar import catalog as module
from voicebar.catalog import CatalogError, load_catalog

APPS = [
    {"Name": "Google Chrome", "AppID": "Chrome"},
    {"Name": "Claude", "AppID": "Claude!App"},
    {"Name": "Autodesk Fusion", "AppID": "Autodesk.Fusion"},
]


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache/startapps.json").write_text(json.dumps(APPS), encoding="utf-8")
    (tmp_path / "config").mkdir()
    files = {
        "aliases.yaml": "browser: Google Chrome\ncloud: Claude\nfusion: Autodesk Fusion\n",
        "sites.yaml": "github: {url: 'https://github.com', title: GitHub}\n",
        "settings.yaml": "name: computer\nfollowup_seconds: 8\nengines:\n  google: 'https://google.com/search?q={q}'\n",
        "pinned.yaml": "[]\n",
    }
    for name, contents in files.items():
        (tmp_path / "config" / name).write_text(contents, encoding="utf-8")
    return tmp_path


def write_config(project, name, text):
    (project / "config" / name).write_text(text, encoding="utf-8")


def test_loads_cached_identities_aliases_sites_and_empty_pinned(project, monkeypatch):
    def no_enumeration():
        pytest.fail("A present valid cache must avoid PowerShell")
    monkeypatch.setattr(module, "_get_start_apps", no_enumeration)
    result = load_catalog(project)
    assert result.catalog.apps == [app["Name"] for app in APPS]
    assert [(app.name, app.app_id) for app in result.installed_apps] == [
        (app["Name"], app["AppID"]) for app in APPS
    ]
    assert result.catalog.aliases["cloud"] == "Claude"
    assert result.catalog.sites == {"github": "https://github.com"}
    assert result.site_titles == {"github": "GitHub"}
    assert result.catalog.engines == {"google": "https://google.com/search?q={q}"}
    assert result.pinned == []


def test_refresh_replaces_cache_and_preserves_unicode(project, monkeypatch):
    new_apps = APPS + [{"Name": "Café", "AppID": "cafe.app"}]
    monkeypatch.setattr(module, "_get_start_apps", lambda: new_apps)
    result = load_catalog(project, refresh=True)
    assert result.installed_apps[-1].name == "Café"
    assert json.loads((project / ".cache/startapps.json").read_text(encoding="utf-8")) == new_apps
    assert load_catalog(project).installed_apps[-1].app_id == "cafe.app"


def test_missing_cache_enumerates_once(project, monkeypatch):
    cache = project / ".cache/startapps.json"
    cache.unlink()
    calls = []
    monkeypatch.setattr(module, "_get_start_apps", lambda: calls.append(True) or APPS)
    load_catalog(project)
    load_catalog(project)
    assert calls == [True]


def test_failed_refresh_preserves_last_good_cache(project, monkeypatch):
    cache = project / ".cache/startapps.json"
    before = cache.read_bytes()
    def failed_provider():
        raise CatalogError("Get-StartApps failed")
    monkeypatch.setattr(module, "_get_start_apps", failed_provider)
    with pytest.raises(CatalogError, match="Get-StartApps failed"):
        load_catalog(project, refresh=True)
    assert cache.read_bytes() == before


@pytest.mark.parametrize("filename,text,match", [
    ("aliases.yaml", "cloud: Claude\ncloud: Google Chrome\n", "duplicate key 'cloud'"),
    ("aliases.yaml", "Cloud: Claude\n' cloud ': Google Chrome\n", "Duplicate phrase"),
    ("aliases.yaml", "vs code: Claude\nvscode: Google Chrome\n", "Duplicate phrase"),
    ("sites.yaml", "Git Hub: https://github.com\ngithub: https://example.com\n", "Duplicate phrase"),
    ("sites.yaml", "cloud: https://example.com\n", "Duplicate phrase"),
    ("aliases.yaml", "missing: Not Installed\n", "not installed"),
    ("aliases.yaml", "chrome: google chrome\n", "not installed"),
    ("aliases.yaml", "[cloud, browser]\n", "must be a mapping"),
    ("aliases.yaml", "cloud: 123\n", "must be nonempty text"),
    ("sites.yaml", "github: {url: 'https://github.com', title: 123}\n", "title must be nonempty text"),
    ("sites.yaml", "github: {url: 'https://github.com', unknown: value}\n", "accepts only url"),
    ("sites.yaml", "github: {title: GitHub}\n", "URL must be nonempty text"),
    ("settings.yaml", "[]\n", "must be a mapping"),
    ("settings.yaml", "engines: []\n", "engines must be a mapping"),
    ("settings.yaml", "engines: {}\nname: 123\n", "name must be nonempty text"),
    ("settings.yaml", "engines: {}\nfollowup_seconds: true\n", "followup_seconds"),
    ("settings.yaml", "engines: {}\nfollowup_seconds: -1\n", "followup_seconds"),
    ("settings.yaml", "engines: {}\nfollowup_seconds: .nan\n", "followup_seconds"),
    ("pinned.yaml", "{}\n", "must be a list"),
    ("pinned.yaml", "[Not Installed]\n", "not installed"),
    ("pinned.yaml", "[Claude, Claude]\n", "duplicate app"),
    ("aliases.yaml", "true: Claude\n", "mapping keys must be text"),
    ("sites.yaml", "github: {url: 'https://github.com', url: 'https://example.com'}\n", "duplicate key 'url'"),
])
def test_invalid_config_is_a_plain_error(project, filename, text, match):
    write_config(project, filename, text)
    with pytest.raises(CatalogError, match=match):
        load_catalog(project)


@pytest.mark.parametrize("url", [
    "example.com", "file:///C:/secret", "javascript:alert(1)", "https://",
    "https://bad host.com", "https://example.com:bad", "https://example.com:70000",
    "https://[broken", "https://example.com/%zz", "https://bad_host.com",
    "https://example.com\\path", "https://example.com/{q}",
])
def test_malformed_site_urls_are_rejected(project, url):
    write_config(project, "sites.yaml", json.dumps({"site": url}))
    with pytest.raises(CatalogError, match="valid http:// or https:// URL"):
        load_catalog(project)


@pytest.mark.parametrize("url", [
    "https://google.com/search", "https://google.com/?q={q}&other={q}",
    "https://google.com/?q={query}", "file:///search?q={q}",
    "https://google.com/?q={q}&other={other}",
])
def test_invalid_engine_templates_are_rejected(project, url):
    write_config(project, "settings.yaml", json.dumps({"engines": {"google": url}}))
    with pytest.raises(CatalogError):
        load_catalog(project)


def test_duplicate_display_names_are_retained_but_alias_cannot_choose_one(project):
    apps = APPS + [{"Name": "Twin", "AppID": "twin.first"}, {"Name": "Twin", "AppID": "twin.second"}]
    (project / ".cache/startapps.json").write_text(json.dumps(apps), encoding="utf-8")
    assert load_catalog(project).catalog.apps.count("Twin") == 2
    write_config(project, "aliases.yaml", "twin: Twin\n")
    with pytest.raises(CatalogError, match="ambiguous app name 'Twin'"):
        load_catalog(project)
    write_config(project, "aliases.yaml", "{}\n")
    write_config(project, "pinned.yaml", "[Twin]\n")
    with pytest.raises(CatalogError, match="ambiguous app name 'Twin'"):
        load_catalog(project)


def test_case_space_equivalent_app_names_are_ambiguous(project):
    apps = APPS + [{"Name": "Twin App", "AppID": "twin.first"}, {"Name": "twinapp", "AppID": "twin.second"}]
    (project / ".cache/startapps.json").write_text(json.dumps(apps), encoding="utf-8")
    write_config(project, "aliases.yaml", "twin: Twin App\n")
    with pytest.raises(CatalogError, match="ambiguous app name"):
        load_catalog(project)


def test_duplicate_ids_are_rejected(project):
    (project / ".cache/startapps.json").write_text(json.dumps(APPS + [APPS[0]]), encoding="utf-8")
    with pytest.raises(CatalogError, match="duplicate AppID 'Chrome'"):
        load_catalog(project)


def test_cache_accepts_windows_utf8_bom(project):
    (project / ".cache/startapps.json").write_text(json.dumps(APPS), encoding="utf-8-sig")
    assert load_catalog(project).installed_apps[0].app_id == "Chrome"


def test_malformed_cache_suggests_refresh(project):
    (project / ".cache/startapps.json").write_text("not json", encoding="utf-8")
    with pytest.raises(CatalogError, match="voicebar apps --refresh"):
        load_catalog(project)


@pytest.mark.parametrize("rows", [{"Name": "One", "AppID": "one"}, [None], [{"Name": "One"}]])
def test_malformed_start_menu_records_are_rejected(project, rows):
    (project / ".cache/startapps.json").write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(CatalogError):
        load_catalog(project)


def test_safe_loader_cannot_construct_python_objects(project):
    write_config(project, "aliases.yaml", "!!python/object:builtins.object {}\n")
    with pytest.raises(CatalogError, match="Cannot read aliases.yaml"):
        load_catalog(project)


def test_native_provider_uses_fixed_command_and_explicit_utf8(monkeypatch):
    seen = {}
    def run(args, **kwargs):
        seen.update(args=args, **kwargs)
        return SimpleNamespace(stdout=json.dumps([{"Name": "Café", "AppID": "café.app"}]))
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._get_start_apps()[0]["Name"] == "Café"
    assert seen["args"][-1] == module._START_APPS_COMMAND
    assert "Get-StartApps" in seen["args"][-1]
    assert Path(seen["args"][0]).name.lower() == "powershell.exe"
    assert seen["shell"] is False
    assert seen["encoding"] == "utf-8-sig"
    assert seen["errors"] == "strict"
    assert seen["check"] is True


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(1, ["powershell"], stderr="Enumeration denied"),
    subprocess.TimeoutExpired(["powershell"], 30),
    FileNotFoundError("powershell.exe"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
])
def test_native_provider_failure_is_a_catalog_error(monkeypatch, error):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(CatalogError):
        module._get_start_apps()


def test_native_provider_malformed_json_is_a_catalog_error(monkeypatch):
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="bad json"))
    with pytest.raises(CatalogError, match="Cannot read Get-StartApps output"):
        module._get_start_apps()


def test_parenthesized_and_plain_phrases_cannot_collide(project):
    write_config(project, "aliases.yaml", "'(cloud)': Claude\ncloud: Google Chrome\n")
    with pytest.raises(CatalogError, match="Duplicate phrase"):
        load_catalog(project)


def test_punctuation_without_a_spoken_word_is_rejected(project):
    write_config(project, "aliases.yaml", "'!!!': Claude\n")
    with pytest.raises(CatalogError, match="must contain a spoken word"):
        load_catalog(project)


@pytest.mark.parametrize("url", [
    "https://user:secret@example.com", "https://user@example.com",
    "https://example.com..", "https://example.com:", "https://example.com:0",
    "https://example.com/\u0001path", "https://example.com/\u007fpath",
])
def test_catalog_url_checks_match_schema_boundaries(project, url):
    write_config(project, "sites.yaml", json.dumps({"site": url}))
    with pytest.raises(CatalogError, match="valid http:// or https:// URL"):
        load_catalog(project)


def test_missing_default_google_engine_is_rejected(project):
    write_config(project, "settings.yaml", "engines:\n  youtube: 'https://youtube.com/search?q={q}'\n")
    with pytest.raises(CatalogError, match="must define 'google'"):
        load_catalog(project)


def test_multiword_engine_is_rejected(project):
    write_config(project, "settings.yaml", "engines:\n  google: 'https://google.com/search?q={q}'\n  you tube: 'https://youtube.com/search?q={q}'\n")
    with pytest.raises(CatalogError, match="must be a single word"):
        load_catalog(project)


def test_site_cannot_hide_behind_an_installed_app_name(project):
    write_config(project, "sites.yaml", "googlechrome: https://example.com\n")
    with pytest.raises(CatalogError, match="Duplicate phrase.*installed app name"):
        load_catalog(project)


def test_alias_cannot_redirect_a_different_installed_app_name(project):
    write_config(project, "aliases.yaml", "googlechrome: Claude\n")
    with pytest.raises(CatalogError, match="Duplicate phrase.*hide installed app"):
        load_catalog(project)


def test_alias_may_repeat_its_own_installed_app_name(project):
    write_config(project, "aliases.yaml", "GOOGLE CHROME: Google Chrome\n")
    assert load_catalog(project).catalog.aliases == {"google chrome": "Google Chrome"}
