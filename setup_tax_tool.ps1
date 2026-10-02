$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

function Get-PythonCommand {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        return [pscustomobject]@{ File = $launcher.Source; Prefix = @("-3") }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        return [pscustomobject]@{ File = $python.Source; Prefix = @() }
    }

    throw "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/ and enable 'Add Python to PATH'."
}

$pythonCommand = Get-PythonCommand
$pythonVersionText = & $pythonCommand.File @($pythonCommand.Prefix) -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0) {
    throw "Python could not be started."
}

$pythonVersion = [version]$pythonVersionText.Trim()
if ($pythonVersion -lt [version]"3.11") {
    throw "Python $pythonVersion is installed, but CoinAccount requires Python 3.11 or newer."
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "[CoinAccount] Creating a private Python environment..."
    & $pythonCommand.File @($pythonCommand.Prefix) -m venv (Join-Path $projectRoot ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Python environment."
    }
}

Write-Host "[CoinAccount] Installing required packages..."
& $venvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Failed to update pip. Check the internet connection."
}

& $venvPython -m pip install --disable-pip-version-check -e $projectRoot
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install CoinAccount dependencies. Check the internet connection."
}

$envPath = Join-Path $projectRoot ".env"
$envExamplePath = Join-Path $projectRoot ".env.example"
if ((-not (Test-Path -LiteralPath $envPath)) -and (Test-Path -LiteralPath $envExamplePath)) {
    Copy-Item -LiteralPath $envExamplePath -Destination $envPath
}

& $venvPython -c "from backpack_tax.app import create_app; app = create_app(testing=True); print('[CoinAccount] Application check passed.')"
if ($LASTEXITCODE -ne 0) {
    throw "CoinAccount was installed, but its startup check failed."
}
