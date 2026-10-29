param([Parameter(Mandatory = $true)][string]$RequestPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

# The Python caller passes literal text in JSON; nothing from the corpus is
# evaluated as PowerShell, SSML, a shell command, or an executable argument.
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($request.sampleRate -ne 16000 -or $request.channels -ne 1 -or $request.sampleWidth -ne 2) {
    throw 'Synthesis format must be 16 kHz mono PCM16.'
}
if ($request.voice -cne 'Microsoft David Desktop') { throw 'Unexpected synthesis voice.' }
$outputRoot = [System.IO.Path]::GetFullPath([string]$request.outputRoot)
$rootPrefix = $outputRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
$paths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$identifiers = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)

# Validate every job before producing a file, so malformed input cannot leave
# a misleading partial corpus before the first reported failure.
foreach ($item in $request.items) {
    if ($item.id -isnot [string] -or $item.id -notmatch '^(command|rejected|context)-\d{3}-rate[02]$') {
        throw 'Invalid synthesis item identifier.'
    }
    if (-not $identifiers.Add($item.id)) { throw 'Duplicate synthesis item identifier.' }
    if ($item.text -isnot [string] -or $item.text.Length -eq 0 -or $item.text.IndexOf([char]0) -ge 0) {
        throw 'Speech items require nonempty text without NUL.'
    }
    if ($item.rate -isnot [int] -or $item.rate -notin @(0, 2)) { throw 'Synthesis rates must be 0 or 2.' }
    $path = [System.IO.Path]::GetFullPath([string]$item.wav)
    if (-not $path.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetDirectoryName($path) -cne $outputRoot -or
        [System.IO.Path]::GetFileName($path) -cne ($item.id + '.wav')) {
        throw 'Synthesis output must be the matching WAV directly under outputRoot.'
    }
    if (-not $paths.Add($path)) { throw 'Duplicate synthesis WAV path.' }
}

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Name -ceq $request.voice })
    if ($voices.Count -ne 1) { throw 'Microsoft David Desktop is not an enabled local voice.' }
    $synth.SelectVoice([string]$request.voice)
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    $count = 0
    foreach ($item in $request.items) {
        $synth.Rate = [int]$item.rate
        try {
            $synth.SetOutputToWaveFile([string]$item.wav, $format)
            $synth.Speak([string]$item.text)
        }
        finally { $synth.SetOutputToNull() }
        $count += 1
    }
    @{ count = $count; voice = [string]$request.voice } | ConvertTo-Json -Compress
}
finally { $synth.Dispose() }
