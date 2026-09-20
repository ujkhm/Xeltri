# Copy KiCad sources from the portable Temp work-tree into the git repo.
# Agent must NOT run this (KiCad sources are human-only writes).
# You run it in PowerShell after Ctrl+S in portable KiCad, then git add/commit.
#
#   powershell -File scripts/sync_portable_kicad_to_repo.ps1
#   powershell -File scripts/sync_portable_kicad_to_repo.ps1 -WhatIf

[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$dst = Join-Path $repoRoot "KiCad\Xeltri"
$src = Join-Path $env:LOCALAPPDATA "Temp\PortableDev\work\Xeltri\KiCad\Xeltri"

if (-not (Test-Path -LiteralPath $src)) {
    Write-Error "Portable KiCad copy not found: $src"
}

if (-not (Test-Path -LiteralPath $dst)) {
    Write-Error "Repo KiCad folder not found: $dst"
}

$patterns = @(
    "*.kicad_pcb",
    "*.kicad_sch",
    "*.kicad_pro",
    "*.kicad_prl",
    "*.kicad_dru",
    "fp-lib-table",
    "sym-lib-table",
    "design-block-lib-table"
)

$copied = 0
Get-ChildItem -LiteralPath $src -File | Where-Object {
    $name = $_.Name
    foreach ($pat in $patterns) {
        if ($name -like $pat) { return $true }
    }
    return $false
} | ForEach-Object {
    $target = Join-Path $dst $_.Name
    if ($PSCmdlet.ShouldProcess($target, "Copy from portable $($_.FullName)")) {
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        Write-Host "copied $($_.Name)"
        $copied++
    }
}

Write-Host ""
Write-Host "Copied $copied file(s) -> $dst"
Write-Host "Next: git status / git add KiCad/Xeltri / commit. Analysis still auto-picks the newer copy."
