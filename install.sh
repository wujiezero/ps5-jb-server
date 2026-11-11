#!/bin/sh
# ps5-jb-server installer entry point.
#
#   sudo ./install.sh              install or upgrade (keeps existing config/data)
#   sudo ./install.sh --dry-run    print every action, change nothing
#   sudo ./install.sh uninstall    stop services and remove installed files
#   ./install.sh doctor            report the current state
#
# Missing system packages (python3 here; openssl, python3-dnspython, nftables…
# in the Python installer) are installed automatically unless --no-deps is given.
#
# One-liner: curl -fsSL https://raw.githubusercontent.com/wujiezero/ps5-jb-server/main/bootstrap.sh | sudo sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

dry_run=0
no_deps=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        --no-deps) no_deps=1 ;;
    esac
done

# pkg_install PACKAGE... — install with whichever package manager exists.
# The first attempt uses the current package lists; a fresh container often
# has none, so a failure refreshes them once and retries.
pkg_install() {
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y --no-install-recommends "$@" \
            || { apt-get update && apt-get install -y --no-install-recommends "$@"; }
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y "$@"
    elif command -v yum >/dev/null 2>&1; then
        yum install -y "$@"
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache "$@"
    elif command -v pacman >/dev/null 2>&1; then
        # Arch names the interpreter "python".
        pacman -Sy --noconfirm --needed $(printf '%s\n' "$@" | sed 's/^python3$/python/')
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install "$@"
    else
        echo "没有识别到包管理器（apt/dnf/yum/apk/pacman/zypper），请手动安装：$*" >&2
        return 1
    fi
}

if ! command -v python3 >/dev/null 2>&1; then
    if [ "$no_deps" -eq 1 ]; then
        echo "需要 python3（例如 apt-get install -y python3）" >&2
        exit 1
    fi
    if [ "$dry_run" -eq 1 ]; then
        echo "  + 将自动安装 python3   (dry-run，python3 不存在，无法继续演练)"
        exit 0
    fi
    if [ "$(id -u)" -ne 0 ]; then
        echo "需要 python3；请用 root 运行（sudo ./install.sh）以便自动安装" >&2
        exit 1
    fi
    echo "未找到 python3，正在自动安装…"
    pkg_install python3 || { echo "python3 安装失败，请手动安装后重试" >&2; exit 1; }
fi
exec python3 "$here/installer/__main__.py" "$@"
