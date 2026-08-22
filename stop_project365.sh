#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"
exec python3 project365_control_service.py stop "$@"
