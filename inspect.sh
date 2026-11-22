#!/usr/bin/env bash
# Purpose: Show Docker inspect details for the app container, then open a shell or run a command inside it.
# Usage: ./inspect.sh [command...]
# Sample: ./inspect.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/app-stack.sh"

standard_inspect_main "$@"
