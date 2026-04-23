param(
    [string]$PythonExe = "python"
)

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

$currentVersion = Get-AppVersion

$blockedSegmentNames = @(
    "__pycache__",
    ".pytest_cache",
    "logs",
    "exports",
    "build",
    "dist",
    "review_snapshot",
    ".git"
)

$cleanScript = Join-Path $projectRoot "clean_packaging.ps1"
$checkScript = Join-Path $projectRoot "check_packaging.ps1"

function Invoke-CheckedCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-Host "Running: $FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-CheckedScript {
    param(
        [string]$ScriptPath,
        [object[]]$Arguments = @()
    )

    Write-Host "Running script: $ScriptPath $($Arguments -join ' ')"
    & $ScriptPath @Arguments
    if (-not $?) {
        throw "Script failed: $ScriptPath $($Arguments -join ' ')"
    }
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Script failed with exit code ${LASTEXITCODE}: $ScriptPath $($Arguments -join ' ')"
    }
}

function Get-RelativePathSegments {
    param([string]$RelativePath)

    if ($null -eq $RelativePath) {
        return @()
    }
    $normalized = [string]$RelativePath
    $normalized = $normalized.Trim().TrimStart("\", "/").Replace("/", "\")
    if (-not $normalized) {
        return @()
    }
    return @($normalized -split "[\\]+" | Where-Object { $_ })
}

function Test-BlockedRelativePath {
    param([string]$RelativePath)

    $segments = Get-RelativePathSegments -RelativePath $RelativePath
    if ($segments.Count -eq 0) {
        return $false
    }

    $fileName = [string]$segments[-1]
    if ($fileName.EndsWith(".pyc", [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    if ([string]::Equals($fileName, "SHA256SUMS.txt", [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }

    foreach ($segment in $segments) {
        foreach ($blockedName in $blockedSegmentNames) {
            if ([string]::Equals([string]$segment, [string]$blockedName, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }

    for ($index = 0; $index -lt ($segments.Count - 1); $index++) {
        if (
            [string]::Equals([string]$segments[$index], "data", [System.StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals([string]$segments[$index + 1], "user_settings.json", [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            return $true
        }
        if (
            [string]::Equals([string]$segments[$index], "docs", [System.StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals([string]$segments[$index + 1], "archive", [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            return $true
        }
    }

    return $false
}

function Test-NonCurrentFieldTrialArtifact {
    param([string]$RelativePath)

    $fileName = [System.IO.Path]::GetFileName([string]$RelativePath)
    if (-not $fileName) {
        return $false
    }

    $versionedPattern = '^(FIELD_TRIAL_(DAILY_LOG|ISSUE_TEMPLATE|NOTES|QUICK_CARD|TEST_PLAN)_v.+\.md|MANIFEST_FIELD_TRIAL_v.+\.txt|RELEASE_CHECKLIST_v.+\.md)$'
    if ($fileName -notmatch $versionedPattern) {
        return $false
    }

    return $fileName -notlike "*$currentVersion*"
}

function Get-AllowedWorkspaceFiles {
    Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force | Where-Object {
        $relative = $_.FullName.Substring($projectRoot.Length).TrimStart("\").Replace("/", "\")
        $fileName = [System.IO.Path]::GetFileName($relative)
        $isChecksumManifest = [string]::Equals($fileName, "SHA256SUMS.txt", [System.StringComparison]::OrdinalIgnoreCase)
        $isArchivedDoc = $relative.StartsWith("docs\archive\", [System.StringComparison]::OrdinalIgnoreCase)
        -not $isChecksumManifest -and
        -not $isArchivedDoc -and
        -not (Test-BlockedRelativePath -RelativePath $relative) -and
        -not (Test-NonCurrentFieldTrialArtifact -RelativePath $relative)
    }
}

$testCommands = @(
    @("-S", "-m", "compileall", "-q", "ygas_monitor", "tests"),
    @("-m", "pytest", "tests/test_protocol.py", "-q"),
    @("-m", "pytest", "tests/test_registry.py", "-q"),
    @("-m", "pytest", "tests/test_services.py", "-q"),
    @("-m", "pytest", "tests/test_session_ui.py", "-q"),
    @("-m", "pytest", "tests/test_charts.py", "-q"),
    @("-m", "pytest", "tests/test_export.py", "-q"),
    @("-m", "pytest", "tests/test_rc1.py", "-q"),
    @("-m", "pytest", "tests/test_release_consistency.py", "-q"),
    @("-m", "pytest", "--collect-only", "tests/test_session_ui.py", "-q"),
    @("-m", "pytest", "-q")
)

$cleanBeforeTests = Join-Path $projectRoot "clean_packaging.ps1"
Invoke-CheckedScript -ScriptPath $cleanBeforeTests

foreach ($arguments in $testCommands) {
    Invoke-CheckedCommand -FilePath $PythonExe -Arguments $arguments
}

Invoke-CheckedScript -ScriptPath $cleanScript
Invoke-CheckedScript -ScriptPath $checkScript

$version = $currentVersion

$distDir = Join-Path $projectRoot "dist"
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$zipPath = Join-Path $distDir "GasAxisStudio_Source_v$version.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::Open($zipPath, "Create")
try {
    foreach ($file in Get-AllowedWorkspaceFiles) {
        $relative = $file.FullName.Substring($projectRoot.Length).TrimStart("\").Replace("\", "/")
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive,
            $file.FullName,
            $relative,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
}
finally {
    $archive.Dispose()
}

Write-Host "Running script: $checkScript -ZipPath $zipPath"
& $checkScript -ZipPath $zipPath
if ($LASTEXITCODE -ne 0) {
    throw "Script failed with exit code ${LASTEXITCODE}: $checkScript -ZipPath $zipPath"
}

Write-Host "Source package built successfully."
Write-Host "Archive path:"
Write-Host $zipPath
Write-Host ""
$uploadOnlyMessage = (
    [string]([char]0x8BF7) + [char]0x53EA + [char]0x4E0A + [char]0x4F20 +
    ' dist/GasAxisStudio_Source_v' + $version + '.zip' +
    [char]0xFF0C + [char]0x4E0D + [char]0x8981 + [char]0x4E0A + [char]0x4F20 +
    [char]0x9879 + [char]0x76EE + [char]0x5DE5 + [char]0x4F5C + [char]0x533A +
    [char]0x538B + [char]0x7F29 + [char]0x5305 + [char]0x3002
)
Write-Host $uploadOnlyMessage -ForegroundColor Yellow
