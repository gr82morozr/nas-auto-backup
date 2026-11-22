#!/usr/bin/env bash
# Purpose: Refuse local changes, then fast-forward pull the latest app repo state.
# Usage: ./checkout.sh
# Sample: ./checkout.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/app-stack.sh"

standard_checkout_master
