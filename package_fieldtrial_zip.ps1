param()

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

function Get-AppVersion {
    $versionFile = Join-Path $projectRoot "ygas_monitor\version.py"
    $versionMatch = Select-String -Path $versionFile -Pattern '^APP_VERSION\s*=\s*["'']([^"'']+)["'']' | Select-Object -First 1
    if ($null -eq $versionMatch -or $versionMatch.Matches.Count -eq 0) {
        throw "Unable to resolve application version from $versionFile"
    }
    return $versionMatch.Matches[0].Groups[1].Value
}

function Write-ChecksumManifest {
    param(
        [string]$Version,
        [string[]]$Files
    )

    $checksumPath = Join-Path $projectRoot "SHA256SUMS.txt"
    $lines = @(
        "# GasAxisStudio field-trial companion checksums for v$Version",
        "# FieldTrial bundle SHA256 is written to the external .sha256 sidecar after bundle creation.",
        ""
    )

    foreach ($file in $Files) {
        $hash = (Get-FileHash -Algorithm SHA256 $file).Hash
        $lines += "$hash  $([System.IO.Path]::GetFileName($file))"
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($checksumPath, $lines, $utf8NoBom)
}

$version = Get-AppVersion
$bundleName = "GasAxisStudio_FieldTrial_v$version"
$sourceZipName = "GasAxisStudio_Source_v$version.zip"
$manifestName = "MANIFEST_FIELD_TRIAL_v$version.txt"
$bundleZipName = "$bundleName.zip"
$sha256SidecarName = "$bundleZipName.sha256"

$distDir = Join-Path $projectRoot "dist"
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

$bundleZipPath = Join-Path $distDir $bundleZipName
$sha256SidecarPath = Join-Path $distDir $sha256SidecarName
$checksumPath = Join-Path $projectRoot "SHA256SUMS.txt"
$checkScript = Join-Path $projectRoot "check_fieldtrial_package.ps1"
$releaseConsistencyScript = Join-Path $projectRoot "check_release_consistency.py"

$filesForChecksum = @(
    (Join-Path $distDir $sourceZipName),
    (Join-Path $projectRoot "RELEASE_CHECKLIST_v$version.md"),
    (Join-Path $projectRoot "FIELD_TRIAL_NOTES_v$version.md"),
    (Join-Path $projectRoot "FIELD_TRIAL_TEST_PLAN_v$version.md"),
    (Join-Path $projectRoot "FIELD_TRIAL_ISSUE_TEMPLATE_v$version.md"),
    (Join-Path $projectRoot "FIELD_TRIAL_DAILY_LOG_v$version.md"),
    (Join-Path $projectRoot "FIELD_TRIAL_QUICK_CARD_v$version.md"),
    (Join-Path $projectRoot "RC_TRIAGE_RULES.md"),
    (Join-Path $projectRoot "README_FIELD_TRIAL.md"),
    (Join-Path $projectRoot $manifestName)
)

foreach ($file in $filesForChecksum) {
    if (-not (Test-Path -LiteralPath $file)) {
        throw "Required FieldTrial package file was not found: $file"
    }
}

Write-ChecksumManifest -Version $version -Files $filesForChecksum

$filesToPackage = @($filesForChecksum + $checksumPath)

if (Test-Path -LiteralPath $bundleZipPath) {
    Remove-Item -LiteralPath $bundleZipPath -Force
}
if (Test-Path -LiteralPath $sha256SidecarPath) {
    Remove-Item -LiteralPath $sha256SidecarPath -Force
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::Open($bundleZipPath, "Create")
try {
    $archive.CreateEntry("$bundleName/") | Out-Null
    foreach ($file in $filesToPackage) {
        $entryName = "$bundleName/$([System.IO.Path]::GetFileName($file))"
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive,
            $file,
            $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
}
finally {
    $archive.Dispose()
}

Write-Host "Running script: $checkScript -ZipPath $bundleZipPath"
& $checkScript -ZipPath $bundleZipPath
if (-not $?) {
    throw "Script failed: $checkScript -ZipPath $bundleZipPath"
}

$bundleHash = (Get-FileHash -Algorithm SHA256 $bundleZipPath).Hash
$bundleHashLine = "$bundleHash  $bundleZipName"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($sha256SidecarPath, $bundleHashLine + [Environment]::NewLine, $utf8NoBom)

if (-not (Test-Path -LiteralPath $releaseConsistencyScript)) {
    throw "Release consistency script was not found: $releaseConsistencyScript"
}
Write-Host "Running: python $releaseConsistencyScript --workspace"
python $releaseConsistencyScript --workspace
if ($LASTEXITCODE -ne 0) {
    throw "Release consistency check failed."
}

Write-Host "FieldTrial package built successfully." -ForegroundColor Green
Write-Host "Archive path:"
Write-Host $bundleZipPath
Write-Host "SHA256:"
Write-Host $bundleHash
Write-Host "SHA256 sidecar:"
Write-Host $sha256SidecarPath
