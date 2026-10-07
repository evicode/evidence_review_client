<#
.SYNOPSIS
    Downloads ffmpeg and places ffmpeg.exe where the application can find it.

.DESCRIPTION
    Fetches a static 64-bit Windows build from BtbN/FFmpeg-Builds, extracts it, and
    copies ffmpeg.exe and ffprobe.exe to vendor\ffmpeg in the repository root.

    Clip export needs it. libmpv is already bundled and can cut a span, but only by
    re-encoding -- its encoder list has no "copy" -- and a re-encoded clip is no longer
    the original recorded data. ffmpeg copies the original streams across untouched,
    which is the difference between handing someone a copy of the evidence and handing
    them a picture of it.

    A static build is used on purpose: one executable with no DLLs to install and
    nothing registered on the machine, so the installer stays a plain per-user copy.

    Integrity: the SHA-256 of the downloaded archive is recorded in
    vendor\ffmpeg\ffmpeg.sha256 on first run and verified on every run after that, so
    a silently changed download is caught. Pass -ExpectedSha256 to pin a known-good
    build from the start.

.PARAMETER Destination
    Where to place the executables. Defaults to <repo>\vendor\ffmpeg.

.PARAMETER ArchiveUrl
    Download a specific archive instead of querying for the latest release.

.PARAMETER ExpectedSha256
    Require the archive to match this hash. Recommended for reproducible builds.

.PARAMETER Force
    Re-download even if ffmpeg.exe is already present.

.EXAMPLE
    .\fetch_ffmpeg.ps1

.EXAMPLE
    .\fetch_ffmpeg.ps1 -ExpectedSha256 A1B2C3... -Force
#>
[CmdletBinding()]
param(
    [string]$Destination,
    [string]$ArchiveUrl,
    [string]$ExpectedSha256,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Destination) { $Destination = Join-Path $repoRoot 'vendor\ffmpeg' }
$ffmpegPath = Join-Path $Destination 'ffmpeg.exe'
$ffprobePath = Join-Path $Destination 'ffprobe.exe'
$hashPath = Join-Path $Destination 'ffmpeg.sha256'

if ((Test-Path $ffmpegPath) -and (Test-Path $ffprobePath) -and -not $Force) {
    $size = [math]::Round((Get-Item $ffmpegPath).Length / 1MB, 1)
    Write-Host "  ffmpeg.exe is already present ($size MB)." -ForegroundColor Green
    Write-Host '  Use -Force to re-download.' -ForegroundColor DarkGray
    return
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

if (-not $ArchiveUrl) {
    # The "lgpl" variant on purpose: the GPL builds carry encoders this application
    # has no use for, and the lighter licence is one less thing for somebody shipping
    # this to a client to have to reason about.
    $releasesApi = 'https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest'
    Write-Host "  Asking GitHub for the latest build..." -ForegroundColor DarkGray
    try {
        $release = Invoke-RestMethod -Uri $releasesApi -Headers @{ 'User-Agent' = 'evidence-review-build' }
    } catch {
        throw "Could not reach the GitHub API: $($_.Exception.Message)`nPass -ArchiveUrl to download a specific build instead."
    }

    $asset = $release.assets |
        Where-Object { $_.name -like 'ffmpeg-n*-win64-lgpl-shared-*.zip' } |
        Select-Object -First 1
    if (-not $asset) {
        $asset = $release.assets |
            Where-Object { $_.name -like '*win64-lgpl*.zip' -and $_.name -notlike '*shared*' } |
            Select-Object -First 1
    }
    if (-not $asset) {
        throw "No win64 lgpl archive in the latest release. Pass -ArchiveUrl explicitly."
    }
    $ArchiveUrl = $asset.browser_download_url
    Write-Host "  $($asset.name)" -ForegroundColor DarkGray
}

$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("evrev-ffmpeg-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $temp | Out-Null
$archive = Join-Path $temp 'ffmpeg.zip'

try {
    Write-Host "  Downloading..." -ForegroundColor DarkGray
    $progress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'   # the bar makes this many times slower
    try {
        Invoke-WebRequest -Uri $ArchiveUrl -OutFile $archive -UseBasicParsing
    } finally {
        $ProgressPreference = $progress
    }

    $actualHash = (Get-FileHash -Path $archive -Algorithm SHA256).Hash
    if ($ExpectedSha256) {
        if ($actualHash -ne $ExpectedSha256.ToUpperInvariant()) {
            throw "The download does not match the expected hash.`n  expected $ExpectedSha256`n  got      $actualHash"
        }
        Write-Host "  Hash matches the one given." -ForegroundColor Green
    } elseif (Test-Path $hashPath) {
        $recorded = (Get-Content $hashPath -Raw).Trim()
        if ($recorded -and $recorded -ne $actualHash) {
            throw "The download no longer matches the hash recorded on the last run.`n  recorded $recorded`n  now      $actualHash`n`nIf this is an intended upgrade, delete $hashPath and run again."
        }
    }

    Write-Host "  Extracting..." -ForegroundColor DarkGray
    $extractDir = Join-Path $temp 'extracted'
    Expand-Archive -Path $archive -DestinationPath $extractDir -Force

    foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
        $found = Get-ChildItem -Path $extractDir -Recurse -Filter $name | Select-Object -First 1
        if (-not $found) { throw "$name was not in the archive." }
        Copy-Item -Path $found.FullName -Destination (Join-Path $Destination $name) -Force
    }

    # Shared builds put the codecs in DLLs beside the executable; they have to come too.
    $dlls = Get-ChildItem -Path $extractDir -Recurse -Filter '*.dll'
    foreach ($dll in $dlls) {
        Copy-Item -Path $dll.FullName -Destination (Join-Path $Destination $dll.Name) -Force
    }

    Set-Content -Path $hashPath -Value $actualHash -Encoding ascii

    $total = [math]::Round(((Get-ChildItem $Destination -File | Measure-Object Length -Sum).Sum / 1MB), 1)
    Write-Host ""
    Write-Host "  ffmpeg is in $Destination ($total MB)" -ForegroundColor Green
    if ($dlls) { Write-Host "  plus $($dlls.Count) shared library file(s)" -ForegroundColor DarkGray }
    Write-Host "  sha256 $actualHash" -ForegroundColor DarkGray
} finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
