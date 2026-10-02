$ErrorActionPreference = "Stop"

$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$distRoot = Join-Path $projectRoot "dist"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archivePath = Join-Path $distRoot "coinaccount-share-$timestamp.zip"

$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stagingRoot = [IO.Path]::GetFullPath((Join-Path $tempRoot ("coinaccount-share-" + [guid]::NewGuid().ToString("N"))))
$packageRoot = Join-Path $stagingRoot "CoinAccount"

if (-not $stagingRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "The temporary package directory is outside the system temporary directory."
}

$shareItems = @(
    ".env.example",
    ".gitignore",
    "pyproject.toml",
    "README.md",
    "setup_tax_tool.cmd",
    "setup_tax_tool.ps1",
    "start_tax_tool.cmd",
    "src",
    "tests"
)

try {
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
    foreach ($relativePath in $shareItems) {
        $sourcePath = Join-Path $projectRoot $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath)) {
            throw "Required sharing file is missing: $relativePath"
        }
        Copy-Item -LiteralPath $sourcePath -Destination $packageRoot -Recurse -Force
    }

    $generatedDirectories = Get-ChildItem -LiteralPath $packageRoot -Directory -Recurse -Force |
        Where-Object { $_.Name -eq "__pycache__" -or $_.Name -like "*.egg-info" } |
        Sort-Object { $_.FullName.Length } -Descending
    foreach ($generatedDirectory in $generatedDirectories) {
        Remove-Item -LiteralPath $generatedDirectory.FullName -Recurse -Force
    }
    Get-ChildItem -LiteralPath $packageRoot -File -Recurse -Filter "*.pyc" -Force |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

    New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
    Compress-Archive -LiteralPath $packageRoot -DestinationPath $archivePath -CompressionLevel Optimal
    Write-Host "[CoinAccount] Privacy-safe sharing package created:"
    Write-Host $archivePath
    Write-Host "The package excludes .env, .venv, databases, imported PDFs/CSVs, and asset logos."
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
