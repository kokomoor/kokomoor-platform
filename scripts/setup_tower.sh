#!/usr/bin/env bash
# Kokomoor Platform — Tower Bootstrap Script
# Run this once on a fresh Ubuntu Server 24.04 to install dependencies.
#
# Usage: bash scripts/setup_tower.sh

set -euo pipefail

echo "=== Kokomoor Platform — Tower Setup ==="

# System packages
echo "[1/5] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-venv python3-pip python3-pytest \
    git curl wget unzip \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64

# Docker (if not already installed)
if ! command -v docker &> /dev/null; then
    echo "[2/5] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "  → Log out and back in for Docker group to take effect."
else
    echo "[2/5] Docker already installed."
fi

# Python virtual environment
echo "[3/5] Setting up Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

# Playwright browsers
echo "[4/5] Installing Playwright browsers..."
playwright install chromium --with-deps

# Environment file
echo "[5/5] Setting up environment..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  → Created .env from template. Edit with your API keys."
else
    echo "  → .env already exists, skipping."
fi

# Create data directory
mkdir -p data

echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "  1. Edit .env with your Anthropic API key"
echo "  2. Run: pytest -v"
echo "  3. Run: python -m pipelines.job_agent"
