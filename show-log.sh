#!/usr/bin/env bash
# Purpose: Tail the Auto Backup container logs.
# Usage: ./show-log.sh [lines]
# Sample: ./show-log.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/app-stack.sh"

standard_show_log_main "${1:-200}"
