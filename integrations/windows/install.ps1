# Installs or updates this fork of the Carvera Controller (branch upload-api) on Windows,
# adds it to the Start menu, and installs the Send to Carvera add-in for Fusion.
#
# Run it from PowerShell (no admin window needed; Windows asks when a tool needs installing):
#   irm https://raw.githubusercontent.com/toddmitchell/Carvera_Controller/upload-api/integrations/windows/install.ps1 | iex
#
# Run it again to update. Optional settings, set before running:
#   $env:CARVERA_DIR     where the code goes (default %LOCALAPPDATA%\CarveraControllerUploadAPI)
#   $env:CARVERA_BRANCH  branch to use (default upload-api)

$ErrorActionPreference = 'Stop'

$Repo      = 'https://github.com/toddmitchell/Carvera_Controller.git'
$Branch    = if ($env:CARVERA_BRANCH) { $env:CARVERA_BRANCH } else { 'upload-api' }
$Dir       = if ($env:CARVERA_DIR) { $env:CARVERA_DIR } else { Join-Path $env:LOCALAPPDATA 'CarveraControllerUploadAPI' }
$Python    = '3.12'   # the version the Controller's own Windows release is built with
$Shortcut  = 'Carvera Controller (Upload API)'

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Need-Winget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget is missing. Install "App Installer" from the Microsoft Store, then run this again.'
    }
}

function Winget-Install($id, $extra = @()) {
    Need-Winget
    winget install --id $id --exact --source winget --accept-source-agreements --accept-package-agreements @extra
    if ($LASTEXITCODE -ne 0) { throw "winget couldn't install $id (exit code $LASTEXITCODE)." }
    Refresh-Path
}

# --- Git -------------------------------------------------------------------------------------
Step 'Git'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Winget-Install 'Git.Git' }
git --version

# --- Python ----------------------------------------------------------------------------------
Step "Python $Python"
function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        # Windows PowerShell 5.1 turns redirected stderr into an error under 'Stop'.
        $ErrorActionPreference = 'Continue'
        $exe = & py "-$Python" -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$($Python -replace '\.', '')\python.exe"
    if (Test-Path $guess) { return $guess }
    return $null
}
$PythonExe = Find-Python
if (-not $PythonExe) {
    Winget-Install "Python.Python.$Python" @('--scope', 'user')
    $PythonExe = Find-Python
    if (-not $PythonExe) { throw "Python $Python was installed but can't be found. Open a new PowerShell window and run this again." }
}
& $PythonExe --version

# --- C++ compiler (one dependency, QuickLZ, has no Windows wheel and is compiled) -------------
Step 'Visual C++ build tools'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
function Has-VC {
    if (-not (Test-Path $vswhere)) { return $false }
    $found = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    return [bool]$found
}
if (-not (Has-VC)) {
    Write-Host 'Installing the Visual Studio Build Tools (C++ only). Windows will ask for permission; this takes a while.'
    Winget-Install 'Microsoft.VisualStudio.2022.BuildTools' @(
        '--override', '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended')
    if (-not (Has-VC)) { throw "The C++ build tools didn't install. Install them from https://aka.ms/vs/17/release/vs_BuildTools.exe (Desktop development with C++), then run this again." }
}
Write-Host 'Found.'

# --- Poetry ----------------------------------------------------------------------------------
Step 'Poetry'
$Poetry = Join-Path $env:APPDATA 'Python\Scripts\poetry.exe'
if (-not (Test-Path $Poetry)) {
    (Invoke-WebRequest -Uri 'https://install.python-poetry.org' -UseBasicParsing).Content | & $PythonExe -
    if (-not (Test-Path $Poetry)) { throw "Poetry didn't install." }
}
& $Poetry --version

# --- The code --------------------------------------------------------------------------------
Step "Carvera Controller ($Branch) in $Dir"
if (Test-Path (Join-Path $Dir '.git')) {
    git -C $Dir fetch origin $Branch
    git -C $Dir checkout $Branch
    git -C $Dir pull --ff-only origin $Branch
    if ($LASTEXITCODE -ne 0) { throw "Couldn't update $Dir. It has local changes or a different history; sort that out or delete the folder, then run this again." }
} else {
    git clone --branch $Branch $Repo $Dir
    if ($LASTEXITCODE -ne 0) { throw "Couldn't clone $Repo." }
}
git -C $Dir log --oneline -1

# --- Dependencies ----------------------------------------------------------------------------
Step 'Controller dependencies'
$env:POETRY_VIRTUALENVS_IN_PROJECT = 'true'   # keeps the environment in $Dir\.venv
Push-Location $Dir
try {
    & $Poetry env use $PythonExe
    if ($LASTEXITCODE -ne 0) { throw 'poetry env use failed.' }
    & $Poetry install --only main
    if ($LASTEXITCODE -ne 0) { throw 'poetry install failed. See the messages above.' }
} finally { Pop-Location }
$PythonW = Join-Path $Dir '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $PythonW)) { throw "Expected $PythonW after installing." }

# --- Start menu ------------------------------------------------------------------------------
Step 'Start menu'
$programs = [Environment]::GetFolderPath('Programs')
$lnkPath = Join-Path $programs "$Shortcut.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $PythonW
$lnk.Arguments = '-m carveracontroller'
$lnk.WorkingDirectory = $Dir
$lnk.IconLocation = Join-Path $Dir 'assets\packaging\icon-src.ico'
$lnk.Description = "Carvera Controller with the Upload API ($Branch)"
$lnk.Save()
Write-Host "Added `"$Shortcut`"."

# --- Fusion add-in ---------------------------------------------------------------------------
Step 'Fusion add-in'
$addIns = Join-Path $env:APPDATA 'Autodesk\Autodesk Fusion 360\API\AddIns'
$source = Join-Path $Dir 'integrations\fusion360\SendToCarvera'
$target = Join-Path $addIns 'SendToCarvera'
New-Item -ItemType Directory -Force -Path $addIns | Out-Null
$existing = Get-Item $target -ErrorAction SilentlyContinue
if ($existing -and $existing.LinkType -eq 'Junction' -and ($existing.Target -contains $source)) {
    Write-Host 'Already installed (linked to the code, so updates arrive with it).'
} elseif ($existing) {
    Write-Warning "$target already exists and isn't this script's link, so it was left alone. Remove it and run this again to install the add-in."
} else {
    # A junction, not a copy: updating the code updates the add-in. No admin rights needed.
    New-Item -ItemType Junction -Path $target -Target $source | Out-Null
    Write-Host "Linked $target"
}

# --- Done ------------------------------------------------------------------------------------
Write-Host "`nDone." -ForegroundColor Green
Write-Host @"
Next:
  1. Quit any other Carvera Controller. Only one can be connected to the machine.
  2. Start "$Shortcut" from the Start menu and turn on Settings > Upload API.
  3. Restart Fusion (or in Utilities > Add-Ins, Stop and Run SendToCarvera).
     The button is at Manufacture > Milling > CARVERA.
Run this script again any time to update.
"@
