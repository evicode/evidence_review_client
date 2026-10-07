<#
.SYNOPSIS
    Downloads libmpv and places mpv-2.dll where the application can find it.

.DESCRIPTION
    Fetches the latest 64-bit libmpv development build from shinchiro/mpv-winbuild-cmake,
    extracts it, and copies the DLL to vendor\mpv\mpv-2.dll in the repository root.

    The application looks for the DLL in vendor\mpv during development and in the
    bundled _internal\mpv folder once installed, so this only needs running once
    per checkout.

    Integrity: the SHA-256 of the downloaded archive is recorded in
    vendor\mpv\libmpv.sha256 on first run and verified on every run after that, so
    a silently changed download is caught. Pass -ExpectedSha256 to pin a specific
    known-good build from the start.

.PARAMETER Destination
    Where to place mpv-2.dll. Defaults to <repo>\vendor\mpv.

.PARAMETER ArchiveUrl
    Download a specific archive instead of querying for the latest release.

.PARAMETER ExpectedSha256
    Require the archive to match this hash. Recommended for reproducible builds.

.PARAMETER Force
    Re-download even if mpv-2.dll is already present.

.EXAMPLE
    .\fetch_libmpv.ps1

.EXAMPLE
    .\fetch_libmpv.ps1 -ExpectedSha256 A1B2C3... -Force
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
if (-not $Destination) { $Destination = Join-Path $repoRoot 'vendor\mpv' }
$dllPath = Join-Path $Destination 'mpv-2.dll'
$hashPath = Join-Path $Destination 'libmpv.sha256'

if ((Test-Path $dllPath) -and -not $Force) {
    $size = [math]::Round((Get-Item $dllPath).Length / 1MB, 1)
    Write-Host "mpv-2.dll is already present ($size MB) at $dllPath" -ForegroundColor Green
    Write-Host 'Use -Force to re-download.' -ForegroundColor DarkGray
    exit 0
}

# ---------------------------------------------------------------------------
# Resolve the archive URL
# ---------------------------------------------------------------------------
if (-not $ArchiveUrl) {
    Write-Host 'Looking up the latest libmpv build...' -ForegroundColor Cyan
    $releasesApi = 'https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest'
    try {
        $release = Invoke-RestMethod -Uri $releasesApi -Headers @{ 'User-Agent' = 'evidence-review-build' }
    }
    catch {
        throw "Could not reach the GitHub API: $($_.Exception.Message)`nPass -ArchiveUrl to download a specific build instead."
    }

    # mpv-dev-* is the SDK archive (DLL + headers). Exclude the i686 and v3 variants.
    $asset = $release.assets |
        Where-Object { $_.name -like 'mpv-dev-x86_64-2*' -and $_.name -notlike '*v3*' } |
        Select-Object -First 1
    if (-not $asset) {
        $asset = $release.assets | Where-Object { $_.name -like 'mpv-dev-x86_64*' } | Select-Object -First 1
    }
    if (-not $asset) {
        throw "No mpv-dev-x86_64 asset found in release '$($release.tag_name)'. Pass -ArchiveUrl manually."
    }

    $ArchiveUrl = $asset.browser_download_url
    Write-Host "Found $($asset.name)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
$temp = Join-Path ([IO.Path]::GetTempPath()) ("libmpv_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp -Force | Out-Null
$archive = Join-Path $temp ([IO.Path]::GetFileName(([Uri]$ArchiveUrl).AbsolutePath))

try {
    Write-Host "Downloading $ArchiveUrl" -ForegroundColor Cyan
    $progressPreferenceOriginal = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'   # a visible progress bar makes this ~10x slower
    Invoke-WebRequest -Uri $ArchiveUrl -OutFile $archive -UseBasicParsing
    $ProgressPreference = $progressPreferenceOriginal

    $actualHash = (Get-FileHash -Path $archive -Algorithm SHA256).Hash
    Write-Host "SHA-256: $actualHash" -ForegroundColor DarkGray

    if ($ExpectedSha256) {
        if ($actualHash -ne $ExpectedSha256.ToUpper()) {
            throw "Hash mismatch.`n  expected $($ExpectedSha256.ToUpper())`n  actual   $actualHash"
        }
        Write-Host 'Hash matches the pinned value.' -ForegroundColor Green
    }
    elseif (Test-Path $hashPath) {
        $recorded = (Get-Content $hashPath -Raw).Trim()
        if ($recorded -and $recorded -ne $actualHash) {
            Write-Warning "This archive differs from the one recorded in $hashPath."
            Write-Warning "  recorded $recorded"
            Write-Warning "  current  $actualHash"
            Write-Warning 'This is expected if upstream published a newer build.'
        }
    }

    # -----------------------------------------------------------------------
    # Extract
    # -----------------------------------------------------------------------
    $extractDir = Join-Path $temp 'extracted'
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null

    if ($archive -like '*.zip') {
        Expand-Archive -Path $archive -DestinationPath $extractDir -Force
    }
    else {
        # These archives use the BCJ2 filter, which pure-Python extractors such as
        # py7zr cannot decode, so a real 7-Zip binary is required.
        $sevenZip = (Get-Command 7z.exe -ErrorAction SilentlyContinue).Source
        if (-not $sevenZip) {
            foreach ($candidate in @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe")) {
                if (Test-Path $candidate) { $sevenZip = $candidate; break }
            }
        }

        if (-not $sevenZip) {
            # 7-Zip publishes a standalone console extractor: one 600 KB exe, no
            # installation, no administrator rights. Fetch it into the temp folder.
            Write-Host '7-Zip not installed; fetching the standalone extractor...' -ForegroundColor Cyan
            $sevenZipRoot = Join-Path $temp '7zr'
            New-Item -ItemType Directory -Path $sevenZipRoot -Force | Out-Null
            $candidate = Join-Path $sevenZipRoot '7zr.exe'
            try {
                $ProgressPreference = 'SilentlyContinue'
                Invoke-WebRequest -Uri 'https://www.7-zip.org/a/7zr.exe' -OutFile $candidate -UseBasicParsing
                $ProgressPreference = 'Continue'
                if ((Get-Item $candidate).Length -gt 100KB) { $sevenZip = $candidate }
            }
            catch {
                Write-Warning "Could not download 7zr.exe: $($_.Exception.Message)"
            }
        }

        if (-not $sevenZip) {
            throw @"
Cannot extract a .7z archive.

Install 7-Zip from https://www.7-zip.org and re-run this script, or download
7zr.exe from https://www.7-zip.org/a/7zr.exe and put it on your PATH.
"@
        }

        Write-Host "Extracting with $(Split-Path -Leaf $sevenZip)..." -ForegroundColor Cyan
        & $sevenZip x $archive "-o$extractDir" -y | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Extraction failed (exit code $LASTEXITCODE)." }
    }

    # -----------------------------------------------------------------------
    # Install the DLL
    # -----------------------------------------------------------------------
    $dll = Get-ChildItem -Path $extractDir -Recurse -Filter '*mpv*.dll' |
        Where-Object { $_.Name -match '^(lib)?mpv-?[12]?\.dll$' } |
        Sort-Object Length -Descending |
        Select-Object -First 1
    if (-not $dll) {
        throw "No libmpv DLL found inside $archive."
    }

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Copy-Item -Path $dll.FullName -Destination $dllPath -Force
    Set-Content -Path $hashPath -Value $actualHash -Encoding ascii

    $sizeMb = [math]::Round((Get-Item $dllPath).Length / 1MB, 1)
    Write-Host ''
    Write-Host "Installed mpv-2.dll ($sizeMb MB)" -ForegroundColor Green
    Write-Host "  -> $dllPath" -ForegroundColor Green
    Write-Host ''
}
finally {
    Remove-Item -Path $temp -Recurse -Force -ErrorAction SilentlyContinue
}
