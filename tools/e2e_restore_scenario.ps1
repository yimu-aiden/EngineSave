<#
.SYNOPSIS
End-to-end check of EngineSave: backup -> permanent cleanup -> reinstall -> restore
(plan section 21 M22), on copies inside a sandbox below .local.

.DESCRIPTION
Runs tools\e2e_restore_scenario.py with the project's .venv. The sample game folder and the
game's real global saves are only read (copied); everything is written below the sandbox
(default .local\e2e), which is emptied first. The cleanup deletes permanently inside the
sandbox; the real recycle bin is never used. The report is <sandbox>\report.md.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File tools\e2e_restore_scenario.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\e2e_restore_scenario.ps1 -WhatIf
#>
param(
    [string]$Sample = "",
    [string]$SavesFrom = "",
    [string]$Sandbox = "",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "No .venv found at $python; create it as the README says (pip install -e .)."
}
$arguments = @((Join-Path $PSScriptRoot "e2e_restore_scenario.py"))
if ($Sample) { $arguments += @("--sample", $Sample) }
if ($SavesFrom) { $arguments += @("--saves-from", $SavesFrom) }
if ($Sandbox) { $arguments += @("--sandbox", $Sandbox) }
if ($WhatIf) { $arguments += "--what-if" }
& $python @arguments
exit $LASTEXITCODE
