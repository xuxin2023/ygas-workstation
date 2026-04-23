param(
    [string]$ZipPath = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$blockedSegmentNames = @(
    "__pycache__",
    ".pytest_cache",
    "logs",
    "exports",
    "review_snapshot"
)

function Get-AppVersion {
    $versionFile = Join-Path $projectRoot "ygas_monitor\version.py"
    $versionMatch = Select-String -Path $versionFile -Pattern '^APP_VERSION\s*=\s*["'']([^"'']+)["'']' | Select-Object -First 1
    if ($null -eq $versionMatch -or $versionMatch.Matches.Count -eq 0) {
        throw "Unable to resolve application version from $versionFile"
    }
    return $versionMatch.Matches[0].Groups[1].Value
}

function Test-IsAscii {
    param([string]$Text)

    if ([string]::IsNullOrEmpty($Text)) {
        return $true
    }
    foreach ($char in $Text.ToCharArray()) {
        if ([int][char]$char -gt 127) {
            return $false
        }
    }
    return $true
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
    }

    return $false
}

function Assert-NoBlockedWorkspacePaths {
    $violations = New-Object System.Collections.Generic.List[string]
    Get-ChildItem -LiteralPath $projectRoot -Recurse -Force | ForEach-Object {
        $normalized = $_.FullName.Substring($projectRoot.Length).TrimStart("\").Replace("/", "\")
        if ($normalized -eq ".git" -or $normalized.StartsWith(".git\", [System.StringComparison]::OrdinalIgnoreCase)) {
            return
        }
        if (Test-BlockedRelativePath -RelativePath $normalized) {
            $violations.Add($normalized)
        }
    }
    if ($violations.Count -gt 0) {
        Write-Host "Packaging check failed. Blocked workspace paths found:" -ForegroundColor Red
        $violations | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
        exit 1
    }
}

function Assert-NoBlockedZipPaths {
    param([string]$ArchivePath)

    if (-not (Test-Path -LiteralPath $ArchivePath)) {
        throw "Zip file was not found: $ArchivePath"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $ArchivePath).Path)
    try {
        $violations = New-Object System.Collections.Generic.List[string]
        $nonAsciiTopLevel = New-Object System.Collections.Generic.List[string]
        foreach ($entry in $archive.Entries) {
            if (Test-BlockedRelativePath -RelativePath $entry.FullName) {
                $violations.Add($entry.FullName.Replace("/", "\"))
            }
            $topLevel = ($entry.FullName -split "[/\\]")[0]
            if (-not (Test-IsAscii -Text $topLevel)) {
                $nonAsciiTopLevel.Add($topLevel)
            }
        }
        if ($violations.Count -gt 0) {
            Write-Host "Packaging check failed. Blocked zip entries found:" -ForegroundColor Red
            $violations | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
            exit 1
        }
        if ($nonAsciiTopLevel.Count -gt 0) {
            Write-Host "Packaging check failed. Zip top-level entries must use ASCII names:" -ForegroundColor Red
            $nonAsciiTopLevel | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
            exit 1
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Assert-ZipNameMatchesVersion {
    param([string]$ArchivePath)

    $version = Get-AppVersion
    $expectedName = "GasAxisStudio_Source_v$version.zip"
    $actualName = Split-Path -Leaf $ArchivePath
    if ($actualName -ne $expectedName) {
        Write-Host "Packaging check failed. Zip filename does not match APP_VERSION." -ForegroundColor Red
        Write-Host " - Expected: $expectedName"
        Write-Host " - Actual:   $actualName"
        exit 1
    }
}

Assert-NoBlockedWorkspacePaths

if ($ZipPath) {
    Assert-ZipNameMatchesVersion -ArchivePath $ZipPath
    Assert-NoBlockedZipPaths -ArchivePath $ZipPath
    Write-Host "Packaging check passed. Workspace and zip contents are clean." -ForegroundColor Green
    exit 0
}

Write-Host "Packaging check passed. No blocked workspace paths found." -ForegroundColor Green
