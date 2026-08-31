$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildPython = Join-Path $ProjectDir ".build-venv\Scripts\python.exe"

Push-Location $ProjectDir
try {
    if (-not (Test-Path -LiteralPath $BuildPython)) {
        python -m venv .build-venv
    }
    & $BuildPython -m pip show PyInstaller *> $null
    if ($LASTEXITCODE -ne 0) {
        & $BuildPython -m pip install --disable-pip-version-check PyInstaller
    }
    & $BuildPython -c "import PySide6" *> $null
    if ($LASTEXITCODE -ne 0) {
        & $BuildPython -m pip install --disable-pip-version-check PySide6-Essentials==6.8.3
    }
    $env:PYTHONNOUSERSITE = "1"
    & $BuildPython -m PyInstaller --noconfirm --clean TokenLedgerNative.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，退出代码 $LASTEXITCODE"
    }
    Write-Host "构建完成：$ProjectDir\dist\TokenLedger\TokenLedger.exe"
}
finally {
    Pop-Location
}
