[CmdletBinding()]
param(
    [string]$Version = $env:PERCH_VERSION,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\Perch'),
    [string]$DownloadBase = $env:PERCH_DOWNLOAD_BASE
)

$ErrorActionPreference = 'Stop'
if (-not $DownloadBase) {
    $DownloadBase = 'https://github.com/soyr-redhat/perch/releases'
}
if (-not $Version) {
    $Version = 'latest'
}
if ($Version -eq 'latest') {
    $ReleaseUrl = "$DownloadBase/latest/download"
} else {
    $ReleaseUrl = "$DownloadBase/download/$Version"
}

$Asset = 'Perch-Setup.exe'
$Stage = Join-Path ([System.IO.Path]::GetTempPath()) ("perch-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Stage | Out-Null
try {
    $Installer = Join-Path $Stage $Asset
    $Checksums = Join-Path $Stage 'SHA256SUMS'
    Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseUrl/$Asset" -OutFile $Installer
    Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseUrl/SHA256SUMS" -OutFile $Checksums

    $Expected = (Get-Content $Checksums | Where-Object { $_ -match ('^([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($Asset) + '$') } | Select-Object -First 1)
    if (-not $Expected) {
        throw 'Release checksum is missing the Windows installer.'
    }
    $ExpectedHash = ($Expected -split '\s+')[0].ToLowerInvariant()
    $ActualHash = (Get-FileHash -Algorithm SHA256 -Path $Installer).Hash.ToLowerInvariant()
    if ($ExpectedHash -ne $ActualHash) {
        throw 'Release checksum verification failed; Perch was not installed.'
    }

    $Process = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/DIR="' + $InstallDir + '"')) -Wait -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "Perch installer exited with code $($Process.ExitCode)."
    }

    $CurrentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $Segments = @($CurrentPath -split ';' | Where-Object { $_ })
    if ($Segments -notcontains $InstallDir) {
        [Environment]::SetEnvironmentVariable('Path', (($Segments + $InstallDir) -join ';'), 'User')
    }
    Write-Output "Installed Perch to $InstallDir"
    Write-Output 'Open a new PowerShell window to use perch-cli.'
} finally {
    Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
}
