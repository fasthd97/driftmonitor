# build.ps1
# ---------
# Packages the Lambda function and its Python dependencies for deployment.
# Terraform calls this automatically via null_resource local-exec when
# source files change.
#
# What it does:
#   1. Creates a clean dist/package/ staging directory
#   2. pip installs dependencies into it (not into your system Python)
#   3. Copies Lambda source files into it
#
# Terraform's archive_file then zips dist/package/ and uploads it to AWS.
# The zip is recreated only when source files or requirements change.

$ErrorActionPreference = "Stop"  # Exit immediately on any error, same as bash set -e

# Resolve paths relative to this script so it works regardless of
# which directory Terraform calls it from
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir    = Split-Path -Parent $ScriptDir
$LambdaDir  = Join-Path $RootDir "lambda"
$DistDir    = Join-Path $RootDir "dist"
$PackageDir = Join-Path $DistDir "package"

Write-Host "=== Building Lambda package ==="
Write-Host "  Lambda source : $LambdaDir"
Write-Host "  Output dir    : $PackageDir"

# Clean previous build to ensure no stale files carry over
Write-Host "  Cleaning previous build..."
if (Test-Path $PackageDir) { Remove-Item $PackageDir -Recurse -Force }
New-Item -ItemType Directory -Path $PackageDir | Out-Null

# Install Python dependencies into the package directory.
# -t installs into the package dir instead of system Python.
# --platform and --python-version ensure Linux-compatible wheels
# since Lambda runs on Amazon Linux even though we build on Windows.
# --only-binary=:all: avoids compilation (required for cross-platform builds).
Write-Host "  Installing dependencies..."
pip install `
    -r "$LambdaDir\requirements.txt" `
    -t $PackageDir `
    --quiet `
    --platform manylinux2014_x86_64 `
    --python-version 3.12 `
    --only-binary=:all: `
    --upgrade

# Copy Lambda source files into the package directory
Write-Host "  Copying Lambda source files..."
Copy-Item "$LambdaDir\*.py" $PackageDir
Copy-Item "$LambdaDir\checks" $PackageDir -Recurse

# Verify the entry point landed correctly — fail loudly if missing
if (-not (Test-Path "$PackageDir\handler.py")) {
    Write-Error "ERROR: handler.py not found in package dir. Build failed."
    exit 1
}

Write-Host "  Package contents:"
Get-ChildItem $PackageDir | Format-Table Name, Length -AutoSize

Write-Host "=== Lambda package built successfully ==="
Write-Host "  Output: $PackageDir"