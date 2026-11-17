# Optional local scanner wrapper. No installation, no LLM requests.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Target,
    [ValidateSet('terminal', 'json', 'markdown', 'sarif')]
    [string]$Format = 'json',
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$scanner = Get-Command 'skillspector' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $scanner) {
    throw 'SkillSpector is not installed, so nothing was scanned (record the scan as NOT_RUN). Install it only after the operator approves.'
}

$resolved = Resolve-Path -LiteralPath $Target -ErrorAction Stop
if ($resolved.Count -ne 1) {
    throw 'Target must resolve to exactly one file or directory.'
}

$arguments = @('scan', $resolved.Path, '--no-llm', '--format', $Format)
if ($OutputPath) {
    $parent = Split-Path -Parent $OutputPath
    if (-not $parent -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'Output directory must already exist; no directories are created by this scanner wrapper.'
    }
    if (Test-Path -LiteralPath $OutputPath) {
        throw 'OutputPath already exists; remove it first so a stale report cannot pass as a new scan.'
    }
    $arguments += @('--output', $OutputPath)
}

& $scanner.Source @arguments
if ($LASTEXITCODE -ne 0) {
    throw "SkillSpector reported failure (exit $LASTEXITCODE). Do not treat the target as scanned."
}
if ($OutputPath -and (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf) -or (Get-Item -LiteralPath $OutputPath).Length -eq 0)) {
    throw 'SkillSpector exited 0 but wrote no report. Record the scan as NOT_RUN.'
}
