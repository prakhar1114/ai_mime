# PowerShell build script for AI Mime on Windows
# PyInstaller → dist\ai_mime\ai_mime.exe
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build_win.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = Resolve-Path "$ScriptDir\.."

Write-Host "==> Checking prerequisites..." -ForegroundColor Green

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Error "ERROR: uv binary not found on PATH. Please install uv before building."
    exit 1
}

$env:UV_BINARY_PATH = $uv.Source
Write-Host "==> Using uv at: $env:UV_BINARY_PATH" -ForegroundColor Gray

Write-Host "==> Running PyInstaller..." -ForegroundColor Green
Set-Location $RepoRoot

pyinstaller scripts/pyinstaller.spec --clean --noconfirm

$ExePath = "$RepoRoot\dist\ai_mime\ai_mime.exe"
if (Test-Path $ExePath) {
    Write-Host "==> Build successful! Binary generated at: $ExePath" -ForegroundColor Green
} else {
    Write-Error "ERROR: PyInstaller completed but binary not found at $ExePath"
    exit 1
}
