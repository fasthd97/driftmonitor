 SH
#!/usr/bin/env bash
# scripts/build.sh
# ----------------
# Packages the Lambda function and its Python dependencies for deployment.
# For use on Linux and Mac. Windows users should use build.ps1 instead.
#
# Terraform calls this automatically via null_resource local-exec when
# source files change. Pass -var="build_script=../scripts/build.sh" on
# Linux/Mac when running terraform apply.
#
# What it does:
#   1. Creates a clean dist/package/ staging directory
#   2. pip installs dependencies into it (not into your system Python)
#   3. Copies Lambda source files into it
#
# Terraform's archive_file then zips dist/package/ and uploads it to AWS.
 
set -e  # Exit immediately on any error
 
# Resolve paths relative to this script so it works regardless of
# which directory Terraform calls it from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
LAMBDA_DIR="$ROOT_DIR/lambda"
DIST_DIR="$ROOT_DIR/dist"
PACKAGE_DIR="$DIST_DIR/package"
 
echo "=== Building Lambda package ==="
echo "  Lambda source : $LAMBDA_DIR"
echo "  Output dir    : $PACKAGE_DIR"
 
# Clean previous build to ensure no stale files carry over
echo "  Cleaning previous build..."
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"
 
# Install Python dependencies into the package directory.
# --platform and --python-version ensure Linux-compatible wheels are
# downloaded even if you're building on macOS. Lambda runs on Amazon
# Linux 2 which is compatible with manylinux2014.
# --only-binary=:all: avoids compilation (required for cross-platform builds).
echo "  Installing dependencies..."
pip install \
  -r "$LAMBDA_DIR/requirements.txt" \
  -t "$PACKAGE_DIR" \
  --quiet \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade
 
# Copy Lambda source files into the package directory
echo "  Copying Lambda source files..."
cp "$LAMBDA_DIR"/*.py "$PACKAGE_DIR/"
cp -r "$LAMBDA_DIR/checks" "$PACKAGE_DIR/checks"
 
# Verify the entry point landed correctly — fail loudly if missing
if [ ! -f "$PACKAGE_DIR/handler.py" ]; then
  echo "ERROR: handler.py not found in package dir. Build failed."
  exit 1
fi
 
echo "  Package contents:"
ls -la "$PACKAGE_DIR/"
 
echo "=== Lambda package built successfully ==="
echo "  Output: $PACKAGE_DIR"