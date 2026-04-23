param()

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

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

Remove-WorkspacePath (Join-Path $projectRoot "build")
Remove-WorkspacePath (Join-Path $projectRoot "dist")
Remove-WorkspacePath (Join-Path $projectRoot "logs")
Remove-WorkspacePath (Join-Path $projectRoot "exports")
Remove-WorkspacePath (Join-Path $projectRoot "review_snapshot")
Remove-WorkspacePath (Join-Path $projectRoot ".pytest_cache")
Remove-WorkspaceFile (Join-Path $projectRoot "data\\user_settings.json")

Get-ChildItem -LiteralPath $projectRoot -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "__pycache__" } |
    ForEach-Object { Remove-WorkspacePath $_.FullName }

Get-ChildItem -LiteralPath $projectRoot -Recurse -File -Force |
    Where-Object { $_.Extension -ieq ".pyc" } |
    ForEach-Object { Remove-WorkspaceFile $_.FullName }

Write-Host "Packaging cleanup finished."
