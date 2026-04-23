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

function Remove-WorkspacePath {
    param([string]$TargetPath)

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        return
    }

    $resolved = (Resolve-Path -LiteralPath $TargetPath).Path
    if (-not $resolved.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the workspace: $resolved"
    }

    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Remove-WorkspaceFile {
    param([string]$TargetPath)

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        return
    }

    $resolved = (Resolve-Path -LiteralPath $TargetPath).Path
    if (-not $resolved.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the workspace: $resolved"
    }

    Remove-Item -LiteralPath $resolved -Force
}

function Ensure-ArchivePlacement {
    param(
        [string]$CurrentVersion
    )

    $versionedPattern = '^(FIELD_TRIAL_(DAILY_LOG|ISSUE_TEMPLATE|NOTES|QUICK_CARD|TEST_PLAN)_v(?<version>[^.]+)\.md|MANIFEST_FIELD_TRIAL_v(?<version>[^.]+)\.txt|RELEASE_CHECKLIST_v(?<version>[^.]+)\.md)$'
    Get-ChildItem -LiteralPath $projectRoot -File | ForEach-Object {
        if ($_.Name -notmatch $versionedPattern) {
            return
        }
        $fileVersion = $Matches["version"]
        if ($fileVersion -eq $CurrentVersion) {
            return
        }
        $archiveDir = Join-Path $projectRoot ("docs\archive\v" + $fileVersion)
        New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null
        Move-Item -LiteralPath $_.FullName -Destination (Join-Path $archiveDir $_.Name) -Force
    }
}

function Prune-DistArtifacts {
    param(
        [string]$CurrentVersion
    )

    $distDir = Join-Path $projectRoot "dist"
    if (-not (Test-Path -LiteralPath $distDir)) {
        return
    }

    $allowed = @(
        "GasAxisStudio_Source_v$CurrentVersion.zip",
        "GasAxisStudio_FieldTrial_v$CurrentVersion.zip",
        "GasAxisStudio_FieldTrial_v$CurrentVersion.zip.sha256"
    )
    Get-ChildItem -LiteralPath $distDir -File | ForEach-Object {
        if ($allowed -notcontains $_.Name) {
            Remove-WorkspaceFile $_.FullName
        }
    }
}

function Test-BlockedSnapshotRelativePath {
    param([string]$RelativePath)

    $normalized = [string]$RelativePath
    $normalized = $normalized.Trim().TrimStart("\", "/").Replace("/", "\")
    if (-not $normalized) {
        return $false
    }

    $segments = @($normalized -split "[\\]+" | Where-Object { $_ })
    if ($segments.Count -eq 0) {
        return $false
    }

    $blockedSegments = @("__pycache__", ".pytest_cache", "build", "logs", "exports", "review_snapshot", ".git")
    foreach ($segment in $segments) {
        foreach ($blocked in $blockedSegments) {
            if ([string]::Equals($segment, $blocked, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }

    $fileName = $segments[-1]
    if ($fileName.EndsWith(".pyc", [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $false
}

function Copy-WorkspaceSnapshot {
    param(
        [string]$CurrentVersion
    )

    $snapshotRoot = Join-Path $projectRoot "review_snapshot"
    $snapshotName = "GasAxisStudio_Workspace_v$CurrentVersion"
    $snapshotPath = Join-Path $snapshotRoot $snapshotName

    Remove-WorkspacePath $snapshotPath
    New-Item -ItemType Directory -Force -Path $snapshotPath | Out-Null

    Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($projectRoot.Length).TrimStart("\").Replace("/", "\")
        if (Test-BlockedSnapshotRelativePath -RelativePath $relative) {
            return
        }
        $destination = Join-Path $snapshotPath $relative
        $destinationDir = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $destinationDir)) {
            New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }

    return $snapshotPath
}

$version = Get-AppVersion

Remove-WorkspacePath (Join-Path $projectRoot "build")
Remove-WorkspacePath (Join-Path $projectRoot "logs")
Remove-WorkspacePath (Join-Path $projectRoot "exports")
Remove-WorkspacePath (Join-Path $projectRoot ".pytest_cache")

Get-ChildItem -LiteralPath $projectRoot -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "__pycache__" } |
    ForEach-Object { Remove-WorkspacePath $_.FullName }

Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force |
    Where-Object { $_.Extension -ieq ".pyc" } |
    ForEach-Object { Remove-WorkspaceFile $_.FullName }

Ensure-ArchivePlacement -CurrentVersion $version
Prune-DistArtifacts -CurrentVersion $version
$snapshotPath = Copy-WorkspaceSnapshot -CurrentVersion $version

Write-Host "Review snapshot cleanup finished."
Write-Host "Snapshot path:"
Write-Host $snapshotPath
