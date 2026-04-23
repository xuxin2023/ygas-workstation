param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$productId = "GasAxisStudio"
$specPath = Join-Path $projectRoot "$productId.spec"
$iconPath = Join-Path $projectRoot "assets\app.ico"
$iconPngPath = Join-Path $projectRoot "assets\app.png"
$iconSvgPath = Join-Path $projectRoot "assets\app.svg"
$buildDir = Join-Path $projectRoot "build"
$distDir = Join-Path $projectRoot "dist"
$pytestCacheDir = Join-Path $projectRoot ".pytest_cache"

function Remove-WorkspacePath {
    param([string]$TargetPath)

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        return
    }

    $resolved = (Resolve-Path -LiteralPath $TargetPath).Path
    if (-not $resolved.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the workspace: $resolved"
    }

    if ((Get-Item -LiteralPath $resolved).PSIsContainer) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    } else {
        Remove-Item -LiteralPath $resolved -Force
    }
}

if (-not (Test-Path -LiteralPath $specPath)) {
    throw "Missing spec file: $specPath"
}

if (-not (Test-Path -LiteralPath $iconPath)) {
    $iconSource = $null
    if (Test-Path -LiteralPath $iconPngPath) {
        $iconSource = $iconPngPath
    } elseif (Test-Path -LiteralPath $iconSvgPath) {
        $iconSource = $iconSvgPath
    }

    if ($null -ne $iconSource) {
        Write-Host "Generating app.ico from source icon..."
        $conversionCode = @"
from pathlib import Path
import sys

from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtCore import QSize, Qt
from PySide6.QtSvg import QSvgRenderer

source = Path(sys.argv[1])
target = Path(sys.argv[2])

app = QGuiApplication([])
target.parent.mkdir(parents=True, exist_ok=True)

if source.suffix.lower() == ".svg":
    size = QSize(256, 256)
    image = QImage(size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer = QSvgRenderer(str(source))
    renderer.render(painter)
    painter.end()
    success = image.save(str(target))
else:
    pixmap = QPixmap(str(source))
    if pixmap.isNull():
        raise RuntimeError(f"Unable to load icon source: {source}")
    success = pixmap.save(str(target))

if not success:
    raise RuntimeError(f"Unable to save ico file: {target}")
"@
        & $PythonExe -c $conversionCode $iconSource $iconPath
    }
}

if (-not (Test-Path -LiteralPath $iconPath)) {
    throw "Missing formal application icon: $iconPath"
}

Write-Host "Cleaning old build artifacts..."
Remove-WorkspacePath $buildDir
Remove-WorkspacePath $distDir
Remove-WorkspacePath $pytestCacheDir

$pycacheDirs = Get-ChildItem -LiteralPath $projectRoot -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "__pycache__" }
foreach ($dir in $pycacheDirs) {
    Remove-WorkspacePath $dir.FullName
}

Write-Host "Installing PyInstaller..."
& $PythonExe -m pip install pyinstaller

$version = (& $PythonExe -c "from ygas_monitor.version import APP_VERSION; print(APP_VERSION, end='')").Trim()
if (-not $version) {
    throw "Unable to resolve application version from ygas_monitor.version.APP_VERSION"
}

Write-Host "Running PyInstaller build for GasAxis Studio..."
& $PythonExe -m PyInstaller --noconfirm $specPath

$artifactDir = Join-Path $distDir $productId
if (-not (Test-Path -LiteralPath $artifactDir)) {
    throw "Expected artifact directory was not created: $artifactDir"
}

$zipName = "${productId}_Windows_x64_v$version.zip"
$zipPath = Join-Path $distDir $zipName
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Write-Host "Creating release archive..."
Compress-Archive -Path $artifactDir -DestinationPath $zipPath -Force

Write-Host "Build finished."
Write-Host "Artifact directory:"
Write-Host (Resolve-Path -LiteralPath $artifactDir).Path
Write-Host "Release archive:"
Write-Host (Resolve-Path -LiteralPath $zipPath).Path
