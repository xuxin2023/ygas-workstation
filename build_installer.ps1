param(
    [string]$PythonExe = "python",
    [string]$InnoCompilerPath = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$appExePath = Join-Path $projectRoot "dist\GasAxisStudio\GasAxisStudio.exe"
$iconPath = Join-Path $projectRoot "assets\app.ico"
$issPath = Join-Path $projectRoot "installer\GasAxisStudio.iss"

function Resolve-IsccPath {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (Test-Path -LiteralPath $RequestedPath) {
            return (Resolve-Path -LiteralPath $RequestedPath).Path
        }
        throw "Specified Inno Setup compiler was not found: $RequestedPath"
    }

    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    throw "Inno Setup Compiler (ISCC.exe) was not found. Install Inno Setup 6 or pass -InnoCompilerPath."
}

foreach ($requiredPath in @($appExePath, $iconPath, $issPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required file is missing: $requiredPath"
    }
}

$version = (& $PythonExe -c "from ygas_monitor.version import APP_VERSION; print(APP_VERSION, end='')").Trim()
if (-not $version) {
    throw "Unable to resolve application version from ygas_monitor.version.APP_VERSION"
}

$isccPath = Resolve-IsccPath -RequestedPath $InnoCompilerPath
$outputBaseFilename = "GasAxisStudio_Setup_x64_v$version"
$outputPath = Join-Path $projectRoot "dist\$outputBaseFilename.exe"

if (Test-Path -LiteralPath $outputPath) {
    Remove-Item -LiteralPath $outputPath -Force
}

Write-Host "Using Inno Setup Compiler:"
Write-Host $isccPath
Write-Host "Building GasAxis Studio installer..."

& $isccPath "/DMyAppVersion=$version" "/DMyOutputBaseFilename=$outputBaseFilename" $issPath

if (-not (Test-Path -LiteralPath $outputPath)) {
    throw "Installer build finished without producing the expected file: $outputPath"
}

$artifact = Get-Item -LiteralPath $outputPath
Write-Host "Installer built successfully."
Write-Host "Installer path:"
Write-Host $artifact.FullName
Write-Host "Installer size:"
Write-Host $artifact.Length
