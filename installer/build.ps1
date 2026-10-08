<#
.SYNOPSIS
    Builds Evidence Review into a Windows installer.

.DESCRIPTION
    One command, four steps:

      1. Create an isolated build virtual environment and install dependencies.
      2. Fetch libmpv and ffmpeg (skipped if vendor\ already has them).
      3. Run PyInstaller, producing installer\dist\EvidenceReview\.
      4. Run Inno Setup, producing installer\dist\EvidenceReviewSetup-<version>.exe.

    If Inno Setup is not installed, step 4 is skipped with a clear message and the
    portable folder from step 3 is still usable.

.PARAMETER SkipInstaller
    Stop after PyInstaller. Useful while iterating.

.PARAMETER SkipLibmpv
    Do not attempt to download libmpv.

.PARAMETER SkipFfmpeg
    Do not attempt to download ffmpeg. Clip export will not work in the build.

.PARAMETER Clean
    Delete the build venv and previous output first.

.PARAMETER Console
    Build with a console window attached so startup errors are visible. For
    diagnosing a bundle that will not start; never ship a console build.

.EXAMPLE
    .\build.ps1

.EXAMPLE
    .\build.ps1 -Clean -SkipInstaller
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$SkipLibmpv,
    [switch]$SkipFfmpeg,
    [switch]$Clean,
    [switch]$Console
)

$ErrorActionPreference = 'Stop'
$startedAt = Get-Date

$here = $PSScriptRoot
$repoRoot = Split-Path -Parent $here
$venv = Join-Path $here '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'
$distDir = Join-Path $here 'dist'
$workDir = Join-Path $here 'build'
$bundleDir = Join-Path $distDir 'EvidenceReview'

function Write-Step([string]$text) {
    Write-Host ''
    Write-Host "==> $text" -ForegroundColor Cyan
}

# Read the version from pyproject.toml so it is defined in exactly one place.
$pyproject = Get-Content (Join-Path $repoRoot 'pyproject.toml') -Raw
if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
    throw 'Could not read the version from pyproject.toml.'
}
$version = $Matches[1]

Write-Host ''
Write-Host "  Evidence Review $version - build" -ForegroundColor White
Write-Host "  $repoRoot" -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
if ($Clean) {
    Write-Step 'Cleaning previous output'
    foreach ($path in @($venv, $distDir, $workDir)) {
        if (Test-Path $path) {
            Remove-Item $path -Recurse -Force
            Write-Host "  removed $path" -ForegroundColor DarkGray
        }
    }
}

# ---------------------------------------------------------------------------
Write-Step 'Preparing the build environment'

if (-not (Test-Path $python)) {
    $systemPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $systemPython) {
        throw 'Python was not found on PATH. Install Python 3.11 or newer (64-bit) and re-run.'
    }

    $arch = & $systemPython -c "import struct;print(struct.calcsize('P')*8)"
    if ($arch.Trim() -ne '64') {
        throw "A 64-bit Python is required; the one on PATH is $($arch.Trim())-bit."
    }

    Write-Host '  creating the build virtual environment...' -ForegroundColor DarkGray
    & $systemPython -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}

Write-Host '  installing dependencies (this takes a minute the first time)...' -ForegroundColor DarkGray
& $python -m pip install --upgrade pip --quiet --disable-pip-version-check
& $python -m pip install --quiet --disable-pip-version-check -e "$repoRoot[build]"
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host '  dependencies ready' -ForegroundColor Green

# ---------------------------------------------------------------------------
Write-Step 'Checking libmpv'

$libmpv = Join-Path $repoRoot 'vendor\mpv\mpv-2.dll'
if (Test-Path $libmpv) {
    $sizeMb = [math]::Round((Get-Item $libmpv).Length / 1MB, 1)
    Write-Host "  present ($sizeMb MB)" -ForegroundColor Green
}
elseif ($SkipLibmpv) {
    Write-Warning '  missing, and -SkipLibmpv was given. Playback will not work in this build.'
}
else {
    Write-Host '  not found; downloading...' -ForegroundColor DarkGray
    & (Join-Path $here 'fetch_libmpv.ps1')
    if (-not (Test-Path $libmpv)) {
        throw "libmpv could not be fetched. Run fetch_libmpv.ps1 manually, or pass -SkipLibmpv to build without playback."
    }
}

# ---------------------------------------------------------------------------
# ffmpeg, for cutting an event out of its recording
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host 'ffmpeg' -ForegroundColor Cyan
$ffmpeg = Join-Path (Join-Path $repoRoot 'vendor') (Join-Path 'ffmpeg' 'ffmpeg.exe')
if (Test-Path $ffmpeg) {
    $sizeMb = [math]::Round(((Get-ChildItem (Split-Path $ffmpeg) -File | Measure-Object Length -Sum).Sum / 1MB), 1)
    Write-Host "  present ($sizeMb MB)" -ForegroundColor Green
}
elseif ($SkipFfmpeg) {
    Write-Warning '  missing, and -SkipFfmpeg was given. Clip export will not work in this build.'
}
else {
    Write-Host '  not found; downloading...' -ForegroundColor DarkGray
    & (Join-Path $here 'fetch_ffmpeg.ps1')
    if (-not (Test-Path $ffmpeg)) {
        throw "ffmpeg could not be fetched. Run fetch_ffmpeg.ps1 manually, or pass -SkipFfmpeg to build without clip export."
    }
}

# ---------------------------------------------------------------------------
Write-Step 'Generating the icon'
& $python (Join-Path $here 'make_icon.py')
if ($LASTEXITCODE -ne 0) { Write-Warning '  icon generation failed; building without a custom icon.' }

# ---------------------------------------------------------------------------
Write-Step 'Running PyInstaller'

if ($Console) {
    Write-Warning '  building with a console window attached (diagnostic build)'
    $env:EVREV_BUILD_CONSOLE = '1'
}
else {
    Remove-Item Env:\EVREV_BUILD_CONSOLE -ErrorAction SilentlyContinue
}

Push-Location $here
try {
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $distDir `
        --workpath $workDir `
        (Join-Path $here 'evidence_review.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
}
finally {
    Pop-Location
}

$exePath = Join-Path $bundleDir 'EvidenceReview.exe'
if (-not (Test-Path $exePath)) {
    throw "PyInstaller reported success but $exePath does not exist."
}

$bundleSize = [math]::Round(
    ((Get-ChildItem $bundleDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1
)
Write-Host "  bundle built: $bundleDir ($bundleSize MB)" -ForegroundColor Green

# Confirm the DLL actually made it into the bundle.
$bundledDll = Join-Path $bundleDir '_internal\mpv\mpv-2.dll'
if (Test-Path $bundledDll) {
    Write-Host '  libmpv bundled correctly' -ForegroundColor Green
}
else {
    Write-Warning '  mpv-2.dll is NOT in the bundle; playback will fail on the target machine.'
}

# ---------------------------------------------------------------------------
if ($SkipInstaller) {
    Write-Step 'Skipping the installer (-SkipInstaller)'
    Write-Host "  run the portable build directly: $exePath" -ForegroundColor Green
}
else {
    Write-Step 'Building the installer with Inno Setup'

    $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        # winget installs Inno Setup per-user under LOCALAPPDATA, which is neither
        # on PATH nor under Program Files, so check there too. Version 7 is listed
        # after 6 because setup.iss targets the 6.x directive set.
        foreach ($candidate in @(
                "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
                "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
                "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
                "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe"
            )) {
            if (Test-Path $candidate) { $iscc = $candidate; break }
        }
    }

    if (-not $iscc) {
        Write-Host ''
        Write-Warning 'Inno Setup 6 was not found, so no setup.exe was produced.'
        Write-Host '  Install it with:  winget install JRSoftware.InnoSetup' -ForegroundColor Yellow
        Write-Host '  or download it from https://jrsoftware.org/isdl.php, then re-run this script.' -ForegroundColor Yellow
        Write-Host "  The portable build is ready meanwhile: $exePath" -ForegroundColor Yellow
    }
    else {
        & $iscc "/DMyAppVersion=$version" "/O$distDir" (Join-Path $here 'setup.iss')
        if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }

        $setupExe = Join-Path $distDir "EvidenceReviewSetup-$version.exe"
        if (Test-Path $setupExe) {
            $setupSize = [math]::Round((Get-Item $setupExe).Length / 1MB, 1)
            Write-Host "  installer built: $setupExe ($setupSize MB)" -ForegroundColor Green
        }
    }
}

# ---------------------------------------------------------------------------
$elapsed = (Get-Date) - $startedAt
Write-Host ''
Write-Host ("  Done in {0:mm\:ss}." -f $elapsed) -ForegroundColor White
Write-Host ''
