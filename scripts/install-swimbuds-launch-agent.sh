#!/bin/bash
# The interactive installer replaced this script.

set -euo pipefail

echo "This installer moved to ./scripts/install.sh" >&2
exec "$(cd "$(dirname "$0")" && pwd)/install.sh"
