"""The elevated helper's failure path is tested with wholly fake adapter cmdlets."""

import json
import subprocess
from pathlib import Path


def test_offline_helper_restores_only_its_originally_up_adapter_on_launch_failure(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/run_stage8_offline.ps1").read_text(encoding="utf-8")
    assert source.startswith("#Requires -RunAsAdministrator\n")
    # The production helper must demand elevation. Only this isolated copy runs
    # without it, and every adapter/process cmdlet is replaced before invocation.
    helper = tmp_path / "helper-under-test.ps1"
    helper.write_text(source.removeprefix("#Requires -RunAsAdministrator\n"), encoding="utf-8")
    (tmp_path / ".venv/Scripts").mkdir(parents=True)
    (tmp_path / ".venv/Scripts/python.exe").write_bytes(b"owned mock; never executable")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/prove_stage8_local.py").write_text("# mock\n")
    (tmp_path / "pyproject.toml").write_text("# mock\n")
    (tmp_path / "session.json").write_text('{"items":[]}\n')
    wrapper = tmp_path / "mocks.ps1"
    wrapper.write_text(r"""param([string]$OwnedRoot, [string]$OwnedHelper)
$global:stage8MockStatus = 'Up'
$global:stage8MockSpy = Join-Path $OwnedRoot 'adapter-calls.txt'
function Get-NetAdapter {
    param([switch]$Physical)
    [PSCustomObject]@{ Name='Wi-Fi'; Status=$global:stage8MockStatus }
    [PSCustomObject]@{ Name='Bluetooth'; Status='Disconnected' }
}
function Disable-NetAdapter {
    param([Parameter(ValueFromPipeline=$true)]$InputObject, [switch]$Confirm)
    process {
        if ($InputObject.Name -cne 'Wi-Fi') { throw 'Wrong mock adapter' }
        [IO.File]::AppendAllText($global:stage8MockSpy, "disable Wi-Fi`n")
        $global:stage8MockStatus = 'Disabled'
    }
}
function Enable-NetAdapter {
    param([Parameter(ValueFromPipeline=$true)]$InputObject, [switch]$Confirm)
    process {
        if ($InputObject.Name -cne 'Wi-Fi') { throw 'Wrong mock adapter' }
        [IO.File]::AppendAllText($global:stage8MockSpy, "enable Wi-Fi`n")
        $global:stage8MockStatus = 'Up'
    }
}
function Start-Process {
    param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,
          [switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)
    if ($WindowStyle -ne 'Hidden') { throw 'Helper did not request a hidden worker' }
    throw 'Simulated owned child launch failure'
}
& $OwnedHelper -ProjectRoot $OwnedRoot -Manifest session.json -Backend moonshine -Device cpu
""", encoding="utf-8")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper), str(tmp_path), str(helper)],
        capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode != 0
    restoration = tmp_path / "docs/evidence/stage8-adapter-restoration.json"
    assert restoration.is_file(), completed.stdout + completed.stderr
    report = json.loads(restoration.read_text(encoding="utf-8-sig"))
    assert report["restoration_requested"] is True
    assert report["restoration_error"] is None
    assert report["failure"] == "Simulated owned child launch failure"
    assert {row["Name"]: row["Status"] for row in report["after"]} == {
        "Wi-Fi": "Up", "Bluetooth": "Disconnected"}
    assert (tmp_path / "adapter-calls.txt").read_text().splitlines() == ["disable Wi-Fi", "enable Wi-Fi"]
