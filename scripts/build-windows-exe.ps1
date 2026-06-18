param(
    [switch]$InstallBuildRequirements
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$AppScript = Join-Path $ProjectRoot "app\activity_tracker.py"
$DistPath = Join-Path $ProjectRoot "release"
$WorkPath = Join-Path $ProjectRoot ".pyinstaller\build"
$SpecPath = Join-Path $ProjectRoot ".pyinstaller"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

Set-Location $ProjectRoot

if ($InstallBuildRequirements) {
    & $PythonExe -m pip install -r requirements-build.txt
}

$pyinstallerVersion = & $PythonExe -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: .\scripts\build-windows-exe.ps1 -InstallBuildRequirements"
}

Write-Host "Using PyInstaller $pyinstallerVersion"

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "TrackTivity" `
    --distpath $DistPath `
    --workpath $WorkPath `
    --specpath $SpecPath `
    $AppScript

$ExePath = Join-Path $DistPath "TrackTivity\TrackTivity.exe"
if (!(Test-Path $ExePath)) {
    throw "Build completed, but the executable was not found at $ExePath"
}

Write-Host ""
Write-Host "Executable created:"
Write-Host $ExePath
Write-Host ""
Write-Host "To start it automatically after login, run:"
Write-Host ".\scripts\install-startup-shortcut.ps1"
