<#
.SYNOPSIS
Create (or with -Uninstall remove) EngineSave.lnk on the Desktop and in the Start menu (D25).

.DESCRIPTION
The shortcut starts .venv\Scripts\enginesave-gui.exe of this checkout (no console window), in
the project folder, with assets\enginesave.ico as its icon. Existing EngineSave.lnk files are
replaced, so running it again is safe; run it again after moving the project folder.
Only the two .lnk files are written or removed: no registry entries, no system settings.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    # Folders to use instead of the Desktop and the Start menu (for testing).
    [string[]]$Folders
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$target = Join-Path $project '.venv\Scripts\enginesave-gui.exe'
$icon = Join-Path $project 'assets\enginesave.ico'
if (-not $Folders) {
    # The shell's Desktop folder (may be redirected, e.g. to OneDrive) and the user's Programs.
    $Folders = @(
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('Programs')
    )
}

if (-not $Uninstall -and -not (Test-Path -LiteralPath $target)) {
    Write-Error ("$target is missing. Create the environment first:`n" +
        "  py -3.14 -m venv .venv`n  .\.venv\Scripts\python -m pip install -e .")
}

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in $Folders) {
    $link = Join-Path $folder 'EngineSave.lnk'
    if ($Uninstall) {
        if (Test-Path -LiteralPath $link) {
            Remove-Item -LiteralPath $link
            Write-Output "removed $link"
        } else {
            Write-Output "not present: $link"
        }
        continue
    }
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $target
    $shortcut.WorkingDirectory = $project
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = "EngineSave - Ren'Py save backup"
    $shortcut.Save()
    Write-Output "created $link"
}
