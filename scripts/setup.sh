#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != Darwin ]]; then echo 'AVO requires macOS.'; exit 1; fi
for dependency in uv node npm; do
  if ! command -v "$dependency" >/dev/null; then echo "Install $dependency first; see https://bowenx307.github.io/AVO/docs.html#requirements"; exit 1; fi
done
uv sync --frozen
npm ci --prefix overlay
if [[ "${1:-}" == "--reviewer" ]]; then
  uv run python scripts/configure_reviewer.py
else
  uv run python scripts/configure_key.py
fi
printf '\nSetup complete. Grant macOS permissions to this terminal, then run ./scripts/start.sh\n'
