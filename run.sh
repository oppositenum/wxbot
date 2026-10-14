#!/usr/bin/env bash
# Backward-compatible entrypoint. New installations should use deploy.sh.
set -Eeuo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy.sh" "$@"
