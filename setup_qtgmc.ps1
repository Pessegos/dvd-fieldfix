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

Write-Host 'DVD FieldFix - portable VapourSynth/QTGMC setup' -ForegroundColor Cyan
Write-Host "Destination: $Target"

$ExistingEnvironment = Test-Path (Join-Path $Target 'python.exe')

New-Item -ItemType Directory -Force -Path $SetupWork | Out-Null
if (Test-Path $BootstrapDir) {
    Remove-Item -LiteralPath $BootstrapDir -Recurse -Force
}

if ($Force -and (Test-Path $Target)) {
    $ResolvedRuntime = [IO.Path]::GetFullPath($Runtime)
    $ResolvedTarget = [IO.Path]::GetFullPath($Target)
    if (-not $ResolvedTarget.StartsWith($ResolvedRuntime, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside .runtime: $ResolvedTarget"
    }
    Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
    $ExistingEnvironment = $false
}

if (-not $ExistingEnvironment) {
    Write-Host 'Downloading the official R76 installer...'
    Invoke-WebRequest -Uri $BootstrapUrl -OutFile $BootstrapZip
    $ActualHash = (Get-FileHash -LiteralPath $BootstrapZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualHash -ne $BootstrapSha256) {
        throw "Unexpected SHA-256 for the R76 bootstrap. Expected $BootstrapSha256, received $ActualHash"
    }

    Expand-Archive -LiteralPath $BootstrapZip -DestinationPath $BootstrapDir -Force
    $Installer = Get-ChildItem -LiteralPath $BootstrapDir -Filter 'Install-Portable-VapourSynth-R76.ps1' | Select-Object -First 1
    if (-not $Installer) {
        throw 'The official archive does not contain Install-Portable-VapourSynth-R76.ps1.'
    }

    Write-Host 'Creating an isolated Python 3.12 + VapourSynth R76 environment...'
    & $Installer.FullName -VSVersion 76 -TargetFolder $Target -PythonVersionMajor 3 -PythonVersionMinor 12 -Unattended
    if ($LASTEXITCODE -ne 0) {
        throw "The official installer exited with code $LASTEXITCODE"
    }
} else {
    Write-Host 'Reusing the existing portable environment.' -ForegroundColor Yellow
}

$Python = Join-Path $Target 'python.exe'
if (-not (Test-Path $Python)) {
    throw 'python.exe was not created in the portable environment.'
}

Write-Host 'Installing vsjetpack 2.0.0, deinterlacing plugins and optional DotKill cleanup...'
& $Python -m pip install --disable-pip-version-check --upgrade setuptools wheel
if ($LASTEXITCODE -ne 0) {
    throw "Could not prepare setuptools/wheel (exit code $LASTEXITCODE)"
}
& $Python -m pip install --disable-pip-version-check --upgrade `
    'vsjetpack[deinterlace]==2.0.0' `
    'vapoursynth-dotkill==3.0' `
    --extra-index-url $JetIndex `
    --no-build-isolation
if ($LASTEXITCODE -ne 0) {
    throw "pip exited with code $LASTEXITCODE"
}

$Lock = Join-Path $Target 'qtgmc-environment.lock.txt'
& $Python -m pip freeze --all | Set-Content -LiteralPath $Lock -Encoding UTF8

$VSPipe = @(
    (Join-Path $Target 'vspipe.bat'),
    (Join-Path $Target 'vspipe.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $VSPipe) {
    throw 'vspipe was not found after installation.'
}

Write-Host ''
Write-Host 'Installation completed.' -ForegroundColor Green
Write-Host "VSPipe: $VSPipe"
Write-Host "Resolved lock file: $Lock"
Write-Host 'Execute: dvd-fieldfix doctor'
