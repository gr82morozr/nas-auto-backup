#!/usr/bin/env bash
set -euo pipefail

APP_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$APP_SCRIPT_DIR/../../apps_common/app-stack-common.sh"
