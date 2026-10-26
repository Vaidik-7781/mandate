#!/usr/bin/env bash
# Build a clean source archive (includes the prebuilt apps/web/dist): dist/mandate-<version>.tar.gz and .zip
set -euo pipefail
cd "$(dirname "$0")/.."
ver=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
name="mandate-$ver"
mkdir -p dist && rm -rf "dist/$name" && mkdir "dist/$name"
tar --exclude='.venv' --exclude='.git' --exclude='dist-ssr' --exclude='node_modules' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='*.db' --exclude='./dist' --exclude='*.egg-info' \
    --exclude='bench/results/raw.jsonl' -cf - . | tar -xf - -C "dist/$name"
(cd dist && tar -czf "$name.tar.gz" "$name" && (zip -qr "$name.zip" "$name" 2>/dev/null || true))
rm -rf "dist/$name"
ls -l dist
