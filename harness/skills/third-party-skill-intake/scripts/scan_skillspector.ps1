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
$scanner = Get-Command 'skillspector' -ErrorAction SilentlyContinue
if (-not $scanner) {
    throw 'SkillSpector is not installed. Review optional/skillspector/README.md and use its opt-in installer if approved.'
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
    $arguments += @('--output', $OutputPath)
}

& $scanner.Source @arguments
if ($LASTEXITCODE -ne 0) {
    throw "SkillSpector reported failure (exit $LASTEXITCODE). Do not treat the target as scanned."
}
