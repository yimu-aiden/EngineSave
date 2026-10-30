<#
.SYNOPSIS
Builds the portable EngineSave folder and zip (plan section 19, M15, D28).

.DESCRIPTION
1. Creates .venv-build with "py -3.14 -m venv" (or reuses it), checks that its Python is the
   version and architecture tools\build-constraints.txt names (1.0.0, D72 R18: the zip is
   called win64), and installs EngineSave with the build extra there, every build tool at the
   exact version of that file. The development .venv is not touched.
2. Runs PyInstaller with tools\enginesave.spec into dist\EngineSave\.
3. Checks the result: both programs, the portable marker, README, CHANGELOG, the icon and
   the Tcl/Tk files (init.tcl, tk.tcl) under _internal.
4. Writes dist\EngineSave-<version>-win64-portable.zip and dist\...zip.sha256
   (tools\package_portable.py; a config folder from a test run is never packed).
5. With -Upload, attaches both files to the GitHub release v<version> (gh release upload).

The version is read from src\enginesave\__init__.py. Only build\, dist\ and .venv-build\
inside the project are written (plus pip's download cache).

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_portable.ps1

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_portable.ps1 -Upload
#>
[CmdletBinding()]
param(
    [switch]$Upload
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

$init = Get-Content -Raw -Path (Join-Path $root 'src\enginesave\__init__.py')
if ($init -notmatch '__version__\s*=\s*"([^"]+)"') { throw 'cannot read __version__' }
$version = $Matches[1]
Write-Host "EngineSave $version"

$venv = Join-Path $root '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'
$dist = Join-Path $root 'dist'
$work = Join-Path $root 'build'
$app = Join-Path $dist 'EngineSave'

Push-Location $root
try {
    if (-not (Test-Path $python)) {
        Write-Host "Creating $venv"
        Invoke-Checked 'py -3.14 -m venv' { py -3.14 -m venv $venv }
    }
    # D72 R18: the interpreter the constraints were made for, 64-bit, before anything is built
    $constraints = Join-Path $root 'tools\build-constraints.txt'
    $wanted = Select-String -Path $constraints -Pattern '^#\s+python (\S+), 64-bit' |
        Select-Object -First 1
    if (-not $wanted) { throw "no python line in $constraints" }
    $wantedVersion = $wanted.Matches[0].Groups[1].Value
    $found = & $python -c "import platform, struct, sys; print(sys.version.split()[0], struct.calcsize('P') * 8, platform.machine())"
    if ($LASTEXITCODE -ne 0) { throw 'cannot run the .venv-build Python' }
    $parts = $found.Trim().Split(' ')
    if ($parts[0] -ne $wantedVersion -or $parts[1] -ne '64' -or $parts[2] -ne 'AMD64') {
        throw ".venv-build has Python $found; the build needs $wantedVersion 64-bit AMD64 (see $constraints; delete .venv-build to recreate it)"
    }
    Write-Host "Python $found"
    Write-Host 'Installing EngineSave with the build extra into .venv-build (pinned tools)'
    Invoke-Checked 'pip install' {
        & $python -m pip install --disable-pip-version-check -c $constraints -e '.[build]'
    }

    Write-Host 'Running PyInstaller'
    Invoke-Checked 'PyInstaller' {
        & $python -m PyInstaller 'tools\enginesave.spec' --noconfirm --clean `
            --distpath $dist --workpath $work
    }

    $expected = @(
        'EngineSave.exe',
        'enginesave-cli.exe',
        'EngineSave.portable',
        'README.md',
        'CHANGELOG.md',
        'LICENSE',
        '_internal\assets\enginesave.ico'
    )
    foreach ($item in $expected) {
        if (-not (Test-Path (Join-Path $app $item))) { throw "missing from the build: $item" }
    }
    $internal = Join-Path $app '_internal'
    foreach ($file in @('init.tcl', 'tk.tcl')) {
        $found = Get-ChildItem -Path $internal -Recurse -Filter $file -File |
            Select-Object -First 1
        if (-not $found) { throw "Tcl/Tk were not collected: no $file under _internal" }
        Write-Host ('found ' + $found.FullName.Substring($app.Length + 1))
    }

    $zip = Join-Path $dist ('EngineSave-' + $version + '-win64-portable.zip')
    Invoke-Checked 'packaging' { & $python 'tools\package_portable.py' $app $zip }
    $checksum = $zip + '.sha256'

    if ($Upload) {
        Write-Host ('Uploading to the release v' + $version)
        Invoke-Checked 'gh release upload' { gh release upload ('v' + $version) $zip $checksum }
    }
    Write-Host "Done: $zip"
}
finally {
    Pop-Location
}
