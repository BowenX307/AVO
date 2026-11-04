#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../overlay"
exec npm start
