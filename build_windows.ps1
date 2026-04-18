param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$buildDir = Join-Path $projectRoot "build"
$distDir = Join-Path $projectRoot "dist"

function Remove-WorkspacePath {
    param([string]$TargetPath)

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        return
    }

    $resolved = (Resolve-Path -LiteralPath $TargetPath).Path
    if (-not $resolved.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝删除工作区之外的路径: $resolved"
    }

    Remove-Item -LiteralPath $resolved -Recurse -Force
}

Write-Host "Cleaning old build artifacts..."
Remove-WorkspacePath $buildDir
Remove-WorkspacePath $distDir

$pycacheDirs = Get-ChildItem -LiteralPath $projectRoot -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "__pycache__" }
foreach ($dir in $pycacheDirs) {
    Remove-WorkspacePath $dir.FullName
}

Write-Host "Installing PyInstaller..."
& $PythonExe -m pip install pyinstaller

Write-Host "Running PyInstaller build..."
& $PythonExe -m PyInstaller --noconfirm .\YGasWorkstation.spec

$artifactDir = Resolve-Path .\dist\YGasWorkstation
Write-Host "Build finished."
Write-Host "Artifact directory:"
Write-Host $artifactDir
