Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = "python"
if (Test-Path ".\.venv\Scripts\python.exe") {
    $python = ".\.venv\Scripts\python.exe"
}
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name ProjectAgent --collect-all customtkinter --collect-all PIL --hidden-import httpx run.py
Write-Host "Built dist\ProjectAgent.exe"
