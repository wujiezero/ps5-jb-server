#!/bin/sh
# ps5-jb-server installer entry point.
#
#   sudo ./install.sh              install or upgrade (keeps existing config/data)
#   sudo ./install.sh --dry-run    print every action, change nothing
#   sudo ./install.sh uninstall    stop services and remove installed files
#   ./install.sh doctor            report the current state
#
# One-liner: curl -fsSL https://raw.githubusercontent.com/wujiezero/ps5-jb-server/main/bootstrap.sh | sudo sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if ! command -v python3 >/dev/null 2>&1; then
    echo "需要 python3（apt-get install -y python3）" >&2
    exit 1
fi
exec python3 "$here/installer/__main__.py" "$@"
