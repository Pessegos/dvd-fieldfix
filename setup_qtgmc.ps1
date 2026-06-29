[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $Root '.runtime'
$Target = Join-Path $Runtime 'vapoursynth-portable'
$SetupWork = Join-Path $Runtime '.setup-r76'
$BootstrapZip = Join-Path $SetupWork 'Install-Portable-VapourSynth-R76.zip'
$BootstrapDir = Join-Path $SetupWork 'bootstrap'
$BootstrapUrl = 'https://github.com/vapoursynth/vapoursynth/releases/download/R76/Install-Portable-VapourSynth-R76.zip'
$BootstrapSha256 = 'c209095dfeb61a28bfd20fbf446f71208776e4556ee674112846791378dbdbf0'
$JetIndex = 'https://jaded-encoding-thaumaturgy.github.io/vs-wheels/simple'

Write-Host 'DVD FieldFix - setup portátil de VapourSynth/QTGMC' -ForegroundColor Cyan
Write-Host "Destino: $Target"

$ExistingEnvironment = Test-Path (Join-Path $Target 'python.exe')

New-Item -ItemType Directory -Force -Path $SetupWork | Out-Null
if (Test-Path $BootstrapDir) {
    Remove-Item -LiteralPath $BootstrapDir -Recurse -Force
}

if ($Force -and (Test-Path $Target)) {
    $ResolvedRuntime = [IO.Path]::GetFullPath($Runtime)
    $ResolvedTarget = [IO.Path]::GetFullPath($Target)
    if (-not $ResolvedTarget.StartsWith($ResolvedRuntime, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Recusa de remover caminho fora de .runtime: $ResolvedTarget"
    }
    Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
    $ExistingEnvironment = $false
}

if (-not $ExistingEnvironment) {
    Write-Host 'A descarregar o instalador oficial R76...'
    Invoke-WebRequest -Uri $BootstrapUrl -OutFile $BootstrapZip
    $ActualHash = (Get-FileHash -LiteralPath $BootstrapZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualHash -ne $BootstrapSha256) {
        throw "SHA-256 inesperado no bootstrap R76. Esperado $BootstrapSha256, recebido $ActualHash"
    }

    Expand-Archive -LiteralPath $BootstrapZip -DestinationPath $BootstrapDir -Force
    $Installer = Get-ChildItem -LiteralPath $BootstrapDir -Filter 'Install-Portable-VapourSynth-R76.ps1' | Select-Object -First 1
    if (-not $Installer) {
        throw 'O arquivo oficial não contém Install-Portable-VapourSynth-R76.ps1.'
    }

    Write-Host 'A criar Python 3.12 + VapourSynth R76 isolados...'
    & $Installer.FullName -VSVersion 76 -TargetFolder $Target -PythonVersionMajor 3 -PythonVersionMinor 12 -Unattended
    if ($LASTEXITCODE -ne 0) {
        throw "O instalador oficial terminou com código $LASTEXITCODE"
    }
} else {
    Write-Host 'A retomar o ambiente portátil já existente.' -ForegroundColor Yellow
}

$Python = Join-Path $Target 'python.exe'
if (-not (Test-Path $Python)) {
    throw 'python.exe não foi criado no ambiente portátil.'
}

Write-Host 'A instalar vsjetpack 2.0.0 e plugins de deinterlace...'
& $Python -m pip install --disable-pip-version-check --upgrade setuptools wheel
if ($LASTEXITCODE -ne 0) {
    throw "Não foi possível preparar setuptools/wheel (código $LASTEXITCODE)"
}
& $Python -m pip install --disable-pip-version-check --upgrade `
    'vsjetpack[deinterlace]==2.0.0' `
    --extra-index-url $JetIndex `
    --no-build-isolation
if ($LASTEXITCODE -ne 0) {
    throw "pip terminou com código $LASTEXITCODE"
}

$Lock = Join-Path $Target 'qtgmc-environment.lock.txt'
& $Python -m pip freeze --all | Set-Content -LiteralPath $Lock -Encoding UTF8

$VSPipe = @(
    (Join-Path $Target 'vspipe.bat'),
    (Join-Path $Target 'vspipe.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $VSPipe) {
    throw 'vspipe não foi encontrado depois da instalação.'
}

Write-Host ''
Write-Host 'Instalação concluída.' -ForegroundColor Green
Write-Host "VSPipe: $VSPipe"
Write-Host "Lock resolvido: $Lock"
Write-Host 'Execute: dvd-fieldfix doctor'
