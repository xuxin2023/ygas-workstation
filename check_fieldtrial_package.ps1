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
    "exports"
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

function Read-ArchiveEntryText {
    param([System.IO.Compression.ZipArchiveEntry]$Entry)

    $stream = $Entry.Open()
    try {
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8, $true)
        try {
            return $reader.ReadToEnd()
        }
        finally {
            $reader.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Get-ArchiveEntrySha256 {
    param([System.IO.Compression.ZipArchiveEntry]$Entry)

    $stream = $Entry.Open()
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $hashBytes = $sha256.ComputeHash($stream)
            return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "")
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Parse-ChecksumManifestText {
    param([string]$Text)

    $checksums = @{}
    foreach ($line in ($Text -split "\r?\n")) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed -split "\s+"
        if ($parts.Count -lt 2) {
            throw "Invalid checksum manifest line: $trimmed"
        }
        $checksums[$parts[-1]] = $parts[0].ToUpper()
    }
    return $checksums
}

function Assert-FieldTrialZip {
    param([string]$ArchivePath)

    if (-not $ArchivePath) {
        throw "ZipPath is required."
    }
    if (-not (Test-Path -LiteralPath $ArchivePath)) {
        throw "Zip file was not found: $ArchivePath"
    }

    $version = Get-AppVersion
    $expectedName = "GasAxisStudio_FieldTrial_v$version.zip"
    $expectedRoot = "GasAxisStudio_FieldTrial_v$version"
    $checksumEntryName = "$expectedRoot/SHA256SUMS.txt"
    $requiredEntries = @(
        "$expectedRoot/GasAxisStudio_Source_v$version.zip",
        "$expectedRoot/RELEASE_CHECKLIST_v$version.md",
        "$expectedRoot/FIELD_TRIAL_NOTES_v$version.md",
        "$expectedRoot/FIELD_TRIAL_TEST_PLAN_v$version.md",
        "$expectedRoot/FIELD_TRIAL_ISSUE_TEMPLATE_v$version.md",
        "$expectedRoot/FIELD_TRIAL_DAILY_LOG_v$version.md",
        "$expectedRoot/FIELD_TRIAL_QUICK_CARD_v$version.md",
        "$expectedRoot/RC_TRIAGE_RULES.md",
        "$expectedRoot/SHA256SUMS.txt",
        "$expectedRoot/README_FIELD_TRIAL.md",
        "$expectedRoot/MANIFEST_FIELD_TRIAL_v$version.txt"
    )

    $actualName = Split-Path -Leaf $ArchivePath
    if ($actualName -ne $expectedName) {
        Write-Host "FieldTrial packaging check failed. Zip filename does not match APP_VERSION." -ForegroundColor Red
        Write-Host " - Expected: $expectedName"
        Write-Host " - Actual:   $actualName"
        exit 1
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $ArchivePath).Path)
    try {
        $entries = @($archive.Entries | Select-Object -ExpandProperty FullName)
        $fileEntries = @($archive.Entries | Where-Object { -not $_.FullName.EndsWith("/") })
        $entryMap = @{}
        foreach ($entry in $fileEntries) {
            $entryMap[$entry.FullName] = $entry
        }
        $slashViolations = New-Object System.Collections.Generic.List[string]
        $blockedViolations = New-Object System.Collections.Generic.List[string]
        $nonAsciiTopLevel = New-Object System.Collections.Generic.List[string]
        $legacySourceEntries = New-Object System.Collections.Generic.List[string]
        $archiveDocEntries = New-Object System.Collections.Generic.List[string]

        foreach ($entryName in $entries) {
            if ($entryName.Contains("\")) {
                $slashViolations.Add($entryName)
            }

            if (Test-BlockedRelativePath -RelativePath $entryName) {
                $blockedViolations.Add($entryName)
            }

            $topLevel = ($entryName -split "[/\\]")[0]
            if (-not (Test-IsAscii -Text $topLevel)) {
                $nonAsciiTopLevel.Add($topLevel)
            }
            if (
                $entryName -like "$expectedRoot/GasAxisStudio_Source_v*.zip" -and
                $entryName -ne "$expectedRoot/GasAxisStudio_Source_v$version.zip"
            ) {
                $legacySourceEntries.Add($entryName)
            }
            if ($entryName -like "$expectedRoot/docs/archive/*") {
                $archiveDocEntries.Add($entryName)
            }
        }

        $topLevels = @($entries | ForEach-Object { ($_ -split "[/\\]")[0] } | Select-Object -Unique)
        $missingEntries = @($requiredEntries | Where-Object { $entries -notcontains $_ })

        if ($slashViolations.Count -gt 0) {
            Write-Host "FieldTrial packaging check failed. Zip entries must use forward slashes only:" -ForegroundColor Red
            $slashViolations | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
            exit 1
        }
        if ($topLevels.Count -ne 1 -or $topLevels[0] -ne $expectedRoot) {
            Write-Host "FieldTrial packaging check failed. Unexpected top-level directory." -ForegroundColor Red
            Write-Host " - Expected: $expectedRoot/"
            Write-Host " - Actual:   $($topLevels -join ', ')"
            exit 1
        }
        if ($nonAsciiTopLevel.Count -gt 0) {
            Write-Host "FieldTrial packaging check failed. Zip top-level entries must use ASCII names:" -ForegroundColor Red
            $nonAsciiTopLevel | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
            exit 1
        }
        if ($blockedViolations.Count -gt 0) {
            Write-Host "FieldTrial packaging check failed. Blocked zip entries found:" -ForegroundColor Red
            $blockedViolations | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
            exit 1
        }
        if ($missingEntries.Count -gt 0) {
            Write-Host "FieldTrial packaging check failed. Missing required entries:" -ForegroundColor Red
            $missingEntries | ForEach-Object { Write-Host " - $_" }
            exit 1
        }
        if ($legacySourceEntries.Count -gt 0) {
            Write-Host "FieldTrial packaging check failed. Legacy source archives are not allowed inside the bundle:" -ForegroundColor Red
            $legacySourceEntries | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
            exit 1
        }
        if ($archiveDocEntries.Count -gt 0) {
            Write-Host "FieldTrial packaging check failed. Archived documents must not be distributed inside the bundle:" -ForegroundColor Red
            $archiveDocEntries | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
            exit 1
        }

        if (-not $entryMap.ContainsKey($checksumEntryName)) {
            Write-Host "FieldTrial packaging check failed. Missing checksum manifest inside the bundle." -ForegroundColor Red
            Write-Host " - Expected: $checksumEntryName"
            exit 1
        }

        $checksumMap = Parse-ChecksumManifestText -Text (Read-ArchiveEntryText -Entry $entryMap[$checksumEntryName])
        $expectedChecksummedNames = @(
            $fileEntries |
                ForEach-Object { [System.IO.Path]::GetFileName($_.FullName) } |
                Where-Object { $_ -and $_ -ne "SHA256SUMS.txt" } |
                Sort-Object -Unique
        )
        $actualChecksummedNames = @($checksumMap.Keys | Sort-Object -Unique)
        $missingChecksums = @($expectedChecksummedNames | Where-Object { $actualChecksummedNames -notcontains $_ })
        $unexpectedChecksums = @($actualChecksummedNames | Where-Object { $expectedChecksummedNames -notcontains $_ })

        if ($missingChecksums.Count -gt 0) {
            Write-Host "FieldTrial packaging check failed. SHA256SUMS.txt is missing packaged files:" -ForegroundColor Red
            $missingChecksums | ForEach-Object { Write-Host " - $_" }
            exit 1
        }
        if ($unexpectedChecksums.Count -gt 0) {
            Write-Host "FieldTrial packaging check failed. SHA256SUMS.txt contains unexpected file entries:" -ForegroundColor Red
            $unexpectedChecksums | ForEach-Object { Write-Host " - $_" }
            exit 1
        }

        foreach ($fileName in $expectedChecksummedNames) {
            $entryName = "$expectedRoot/$fileName"
            $expectedHash = [string]$checksumMap[$fileName]
            $actualHash = Get-ArchiveEntrySha256 -Entry $entryMap[$entryName]
            if ($expectedHash -ne $actualHash) {
                Write-Host "FieldTrial packaging check failed. SHA256SUMS.txt does not match packaged content:" -ForegroundColor Red
                Write-Host " - File:     $fileName"
                Write-Host " - Expected: $expectedHash"
                Write-Host " - Actual:   $actualHash"
                exit 1
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

Assert-FieldTrialZip -ArchivePath $ZipPath
Write-Host "FieldTrial packaging check passed. Zip entries use forward slashes, archived docs stay out, and SHA256SUMS.txt matches packaged files." -ForegroundColor Green
