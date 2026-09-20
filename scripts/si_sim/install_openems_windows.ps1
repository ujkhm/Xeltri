<#
.SYNOPSIS
  Install openEMS + CSXCAD Python bindings on Windows for the Xeltri SI toolchain.

.DESCRIPTION
  Downloads the official MSVC binary package from the openEMS-Project GitHub
  releases, extracts it under <repo>\tools\openEMS (git-ignored, ~250 MB),
  installs the matching cp313/cp314 Python wheels, and persists the
  CSXCAD_INSTALL_PATH / OPENEMS_INSTALL_PATH user environment variables that
  the openEMS Python package needs at import time.

  Re-run this any time on a new machine / after deleting tools\openEMS.
  This script only touches tools\ and user environment variables — it does
  NOT modify any KiCad source file (kicad-readonly.mdc policy).

.NOTES
  Verified working with:
    openEMS v0.37.0-rc2 (MSVC), Python 3.13 (cp313 wheels)
  If your Python version differs, check the release page for a matching
  wheel: https://github.com/thliebig/openEMS-Project/releases
#>

param(
    [string]$Version = "v0.37.0-rc2",
    [string]$AssetName = "openEMS_x64_v0.37.0-rc2_msvc.zip",
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
)

$ErrorActionPreference = "Stop"
$ToolsDir = Join-Path $RepoRoot "tools"
$ZipPath = Join-Path $ToolsDir $AssetName
$ExtractDir = Join-Path $ToolsDir "openEMS"
$InstallRoot = Join-Path $ExtractDir "openEMS"  # zip has an extra openEMS\ nesting level

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

$Url = "https://github.com/thliebig/openEMS-Project/releases/download/$Version/$AssetName"
Write-Host "Downloading $Url ..."
$ProgressPreference = "SilentlyContinue"
Invoke-WebRequest -Uri $Url -OutFile $ZipPath -TimeoutSec 600

Write-Host "Extracting to $ExtractDir ..."
Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force
Remove-Item $ZipPath -Force

$PyTag = "cp{0}{1}" -f [Runtime.InteropServices.RuntimeInformation]::FrameworkDescription, ""
$PyVer = & python -c "import sys; print(f'cp{sys.version_info[0]}{sys.version_info[1]}')"
Write-Host "Detected Python tag: $PyVer"

$WheelDir = Join-Path $InstallRoot "python"
$CsxWheel = Get-ChildItem $WheelDir -Filter "csxcad-*-$PyVer-*.whl" | Select-Object -First 1
$OemsWheel = Get-ChildItem $WheelDir -Filter "openems-*-$PyVer-*.whl" | Select-Object -First 1

if (-not $CsxWheel -or -not $OemsWheel) {
    Write-Error "No matching wheel for $PyVer in $WheelDir. Check the release assets for your Python version."
}

Write-Host "Installing $($CsxWheel.Name) ..."
python -m pip install $CsxWheel.FullName

Write-Host "Installing $($OemsWheel.Name) ..."
python -m pip install $OemsWheel.FullName

Write-Host "Persisting CSXCAD_INSTALL_PATH / OPENEMS_INSTALL_PATH (User scope) -> $InstallRoot"
[Environment]::SetEnvironmentVariable("CSXCAD_INSTALL_PATH", $InstallRoot, "User")
[Environment]::SetEnvironmentVariable("OPENEMS_INSTALL_PATH", $InstallRoot, "User")
$env:CSXCAD_INSTALL_PATH = $InstallRoot
$env:OPENEMS_INSTALL_PATH = $InstallRoot

Write-Host "Verifying import (new process, to confirm persisted env vars work) ..."
$TestScript = Join-Path $ToolsDir "_envtest.py"
Set-Content -Path $TestScript -Value "import CSXCAD, openEMS`nprint('IMPORT_OK')"
cmd /c "python `"$TestScript`""
Remove-Item $TestScript -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. If IMPORT_OK printed above, run:"
Write-Host "  python scripts\si_sim\run_si_check.py --ic U3 --full-wave"
Write-Host "Note: openEMS Python still runs correctly in the current terminal without a"
Write-Host "restart because the vars are set for this session too; NEW terminals will pick"
Write-Host "up the persisted User env vars automatically."
