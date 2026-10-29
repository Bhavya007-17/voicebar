#Requires -RunAsAdministrator
<#
Run only after the user approves the Wi-Fi interruption. This helper disables
only the exact, previously-up physical adapter named below and restores it in
finally. It never changes firewall rules, other adapters or existing apps.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [string]$AdapterName = 'Wi-Fi',
    [string]$Manifest = 'work/stage8-session.json',
    [ValidateSet('qwen', 'moonshine', 'moonshine-medium', 'parakeet', 'whisper-large', 'whisper-turbo')]
    [string]$Backend = 'qwen',
    [ValidateSet('cpu', 'cuda')][string]$Device = 'cuda',
    [ValidateRange(30, 1800)][int]$TimeoutSeconds = 600
)
$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathRooted($ProjectRoot)) {
    throw 'ProjectRoot must be an absolute path.'
}
$stage8Root = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
$stage8Python = Join-Path $stage8Root '.venv\Scripts\python.exe'
$stage8Proof = Join-Path $stage8Root 'scripts\prove_stage8_local.py'
if (-not (Test-Path -LiteralPath $stage8Python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $stage8Proof -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $stage8Root 'pyproject.toml') -PathType Leaf)) {
    throw 'ProjectRoot must contain this project, its proof script and its configured Python.'
}
$stage8Manifest = if ([IO.Path]::IsPathRooted($Manifest)) {
    (Resolve-Path -LiteralPath $Manifest).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $stage8Root $Manifest)).Path
}
if (-not $stage8Manifest.StartsWith($stage8Root + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The replay manifest must be inside ProjectRoot.'
}
$stage8Evidence = Join-Path $stage8Root 'docs\evidence'
[IO.Directory]::CreateDirectory($stage8Evidence) | Out-Null
$stage8Report = Join-Path $stage8Evidence ('stage8-network-' + $Backend + '-offline.json')
$stage8Stdout = Join-Path $stage8Evidence 'stage8-offline-output.txt'
$stage8Stderr = Join-Path $stage8Evidence 'stage8-offline-stderr.txt'
$stage8Restoration = Join-Path $stage8Evidence 'stage8-adapter-restoration.json'
$stage8Before = @(Get-NetAdapter -Physical | Select-Object Name, Status)
$stage8Selected = @(Get-NetAdapter -Physical | Where-Object { $_.Name -ceq $AdapterName })
if ($stage8Selected.Count -ne 1) {
    throw ('Expected one exact physical adapter named ' + $AdapterName)
}
$stage8OtherUp = @($stage8Before | Where-Object { $_.Name -cne $AdapterName -and $_.Status -eq 'Up' })
if ($stage8OtherUp.Count -ne 0) {
    throw 'Another physical adapter is up. The helper will not change it or claim offline success.'
}
$stage8Restore = $false
$stage8Child = $null
$stage8Exit = 1
$stage8Failure = $null
$stage8RestoreFailure = $null
try {
    if ($stage8Selected[0].Status -eq 'Up') {
        # Set the restoration obligation before invoking the disabling cmdlet.
        $stage8Restore = $true
        $stage8Selected[0] | Disable-NetAdapter -Confirm:$false
    }
    $stage8Arguments = @($stage8Proof, '--root', $stage8Root, '--manifest', $stage8Manifest,
                         '--backend', $Backend, '--device', $Device, '--output', $stage8Report,
                         '--timeout', [string]$TimeoutSeconds, '--expect-offline')
    # Every argument is a validated fixed value or Windows file path, none can
    # contain a double quote or end in a slash. No shell interprets this string.
    $stage8Quoted = ($stage8Arguments | ForEach-Object {
        if ($_.Contains('"') -or $_.EndsWith('\')) { throw 'Unsupported argument path.' }
        '"' + $_ + '"'
    }) -join ' '
    $stage8Child = Start-Process -FilePath $stage8Python -ArgumentList $stage8Quoted `
        -WorkingDirectory $stage8Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stage8Stdout -RedirectStandardError $stage8Stderr
    if (-not $stage8Child.WaitForExit(($TimeoutSeconds + 60) * 1000)) {
        # PID is the still-live Process object returned by our Start-Process.
        # /T cleans only its descendant tree, including the venv redirector.
        & "$env:SystemRoot\System32\taskkill.exe" /PID $stage8Child.Id /T /F | Out-Null
        throw 'Offline proof exceeded its bounded timeout; its owned process tree was stopped.'
    }
    $stage8Child.Refresh()
    $stage8Exit = $stage8Child.ExitCode
} catch {
    $stage8Failure = $_.Exception.Message
} finally {
    if ($null -ne $stage8Child -and -not $stage8Child.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $stage8Child.Id /T /F | Out-Null
    }
    if ($stage8Restore) {
        try {
            Get-NetAdapter -Physical | Where-Object { $_.Name -ceq $AdapterName } |
                Enable-NetAdapter -Confirm:$false
        } catch {
            $stage8RestoreFailure = $_.Exception.Message
        }
    }
    $stage8After = @(Get-NetAdapter -Physical | Select-Object Name, Status)
    [ordered]@{
        adapter = $AdapterName; before = $stage8Before; after = $stage8After
        restoration_requested = $stage8Restore; restoration_error = $stage8RestoreFailure
        proof_exit_code = $stage8Exit; failure = $stage8Failure; proof_report = $stage8Report
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $stage8Restoration -Encoding UTF8
}
Get-Content -LiteralPath $stage8Stdout -ErrorAction SilentlyContinue
if ($stage8Failure) { Write-Error $stage8Failure }
if ($stage8RestoreFailure) { Write-Error ('Wi-Fi restoration failed: ' + $stage8RestoreFailure) }
exit $stage8Exit
