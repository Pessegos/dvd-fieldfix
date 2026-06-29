[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $Root
try {
    python -m pip install -e '.[gui,dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install build dependencies.' }

    python -m PyInstaller --noconfirm --clean --windowed `
        --name 'DVD-FieldFix' `
        --paths (Join-Path $Root 'src') `
        --collect-all tkinterdnd2 `
        (Join-Path $Root 'packaging\gui_entry.py')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create DVD-FieldFix.exe.' }

    python -m PyInstaller --noconfirm --clean --console `
        --name 'DVD-FieldFix-CLI' `
        --paths (Join-Path $Root 'src') `
        (Join-Path $Root 'packaging\cli_entry.py')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create DVD-FieldFix-CLI.exe.' }

    Copy-Item -LiteralPath (Join-Path $Root 'dist\DVD-FieldFix-CLI\DVD-FieldFix-CLI.exe') -Destination (Join-Path $Root 'dist\DVD-FieldFix\DVD-FieldFix-CLI.exe') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'setup_qtgmc.ps1') -Destination (Join-Path $Root 'dist\DVD-FieldFix') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'qtgmc-requirements.txt') -Destination (Join-Path $Root 'dist\DVD-FieldFix') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'README.md') -Destination (Join-Path $Root 'dist\DVD-FieldFix') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'LICENSE') -Destination (Join-Path $Root 'dist\DVD-FieldFix') -Force
    Write-Host "Build completed in $Root\dist" -ForegroundColor Green
} finally {
    Pop-Location
}
