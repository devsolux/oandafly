# Oandafly 1.0.0 — native Windows AMD64 desktop development.
# No signing secret, API token, automatic execution policy change or Bash dependency.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = $PSScriptRoot
$CommandName = if ($args.Count -gt 0) { [string]$args[0] } else { 'run' }
$Rest = if ($args.Count -gt 1) { @($args[1..($args.Count - 1)]) } else { @() }
if ($CommandName -in @('help', '-h', '--help')) {
    Write-Output @'
Oandafly 1.0.0 — Windows AMD64 desktop
.\dev.ps1 setup --resolve [--python C:\absolute\python.exe] [--offline]
.\dev.ps1                         Open desktop; no automatic worker/account start.
.\dev.ps1 doctor                  Desktop prerequisites.
.\dev.ps1 doctor-worker           Worker prerequisites; Linux containment remains required.
.\dev.ps1 test [pytest arguments]  Tests; no sync on normal runs.
.\dev.ps1 check                   Source checks, Ruff and tests.
.\dev.ps1 build-unsigned          Complete unsigned production pipeline.
.\dev.ps1 build                   Same gates plus Windows signing.
.\dev.ps1 release                 Exact alias of build.
.\dev.ps1 verify PATH             Compiled-window verification.
.\dev.ps1 lock [--check]           Resolve or check genuine locks.
.\dev.ps1 worker --config PATH    Foreground qualified worker, never a bypass.
.\dev.ps1 package                 Complete source ZIP.
.\dev.ps1 clean [--all]            Checkout caches only; app data/locks preserved.

Install native 64-bit CPython 3.13 with Tcl/Tk and uv first. No Python downloads.
PowerShell must already permit scripts; this launcher does not change execution policy.
First setup --resolve generates absent locks; failures never produce placeholder locks.
'@
    exit 0
}
if ($CommandName -in @('build', 'build-unsigned', 'release')) {
    if ($Rest.Count -eq 1 -and $Rest[0] -in @('--help', '-h')) {
        Write-Output "$CommandName takes no arguments. Full production gates; signing uses environment settings."
        exit 0
    }
    if ($Rest.Count -ne 0) { throw "$CommandName takes no arguments." }
}
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -or $env:PROCESSOR_ARCHITEW6432) {
    throw 'Use a native 64-bit Windows AMD64 PowerShell session.'
}
if (-not (Test-Path -LiteralPath (Join-Path $Root 'pyproject.toml'))) { throw 'Run from the Oandafly source project.' }
$Selected = $null
if ($CommandName -eq 'setup') {
    for ($i = 0; $i -lt $Rest.Count; $i++) {
        if ($Rest[$i] -eq '--python') {
            if (($i + 1) -ge $Rest.Count) { throw '--python needs an absolute executable.' }
            $Selected = [string]$Rest[$i + 1]
            if (-not [IO.Path]::IsPathRooted($Selected) -or -not (Test-Path -LiteralPath $Selected -PathType Leaf)) {
                throw '--python needs an absolute executable.'
            }
            break
        }
    }
}
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$VenvDirectory = Join-Path $Root '.venv'
if (Test-Path -LiteralPath $VenvDirectory) {
    if ((Get-Item -LiteralPath $VenvDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Refusing a symlink/junction development environment.'
    }
}
if (-not $Selected -and -not ($CommandName -eq 'clean' -and $Rest -contains '--all') -and (Test-Path -LiteralPath $VenvPython)) { $Selected = $VenvPython }
if (-not $Selected) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Selected = (& py -3.13 -c 'import sys; print(sys.executable)' | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0) { throw 'Install native Python 3.13 with Tcl/Tk.' }
    } else { throw 'Install native Python 3.13 and its Python launcher, or pass setup --python.' }
}
Push-Location $Root
try {
    & $Selected -I -B (Join-Path $Root 'scripts\dev.py') $CommandName @Rest
    $Code = $LASTEXITCODE
} finally { Pop-Location }
exit $Code
