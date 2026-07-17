#!/bin/sh
# Remove build caches and generated artifacts
# Usage: ./clean.sh

set -e

cd "$(dirname "$0")"

rm -rf build dist *.egg-info src/*.egg-info \
       .mypy_cache .pytest_cache \
       src/kiro_crew/static/dist \
       website/dist website/tsconfig.app.tsbuildinfo

find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -name ".DS_Store" -delete 2>/dev/null || true

echo "✅ Clean complete"
