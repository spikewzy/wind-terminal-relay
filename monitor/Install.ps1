# Compatibility entry. Use Update.cmd from the separately extracted console ZIP.
$ErrorActionPreference = 'Stop'
$updater = Join-Path (Split-Path $PSScriptRoot -Parent) 'Update.ps1'
if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) {
    throw 'Extract the complete console ZIP into a separate folder and double-click Update.cmd there.'
}
& $updater @args
