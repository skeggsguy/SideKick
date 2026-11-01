#!/usr/bin/env bash
# Build the harness-sandbox Docker image used by sandbox_run.
# Run this after editing docker/sandbox/Dockerfile (e.g. to add a library).
# Needs network (to pip install at build time) and a running Docker engine
# (OrbStack: `orbctl start`) — see docs/sandbox-setup.md.
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

echo "==> Building harness-sandbox:latest..."
docker build -t harness-sandbox:latest docker/sandbox

echo "==> Verifying the pre-baked libs import (and network is still blocked)..."
docker run --rm --network none harness-sandbox:latest \
  python -c "import numpy, pandas, matplotlib, yaml, PIL, tabulate, openpyxl; print('OK: sandbox libs import')"

echo "Done. sandbox_run will now use harness-sandbox:latest."
