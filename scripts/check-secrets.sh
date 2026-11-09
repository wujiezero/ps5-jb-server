#!/bin/sh
# Refuse to publish anything that looks like private deployment data.
#   ./scripts/check-secrets.sh            # scan the working tree
#   ./scripts/check-secrets.sh --staged   # scan files staged for commit
set -eu
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--staged" ]; then
    files=$(git diff --cached --name-only --diff-filter=ACMR)
else
    files=$(git ls-files 2>/dev/null || find . -type f -not -path './.git/*')
fi

fail=0
scan() {
    label=$1; pattern=$2
    hits=$(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -nIE "$pattern" 2>/dev/null \
        | grep -vE 'scripts/check-secrets.sh|\.md:.*示例' || true)
    if [ -n "$hits" ]; then
        printf '  [%s]\n' "$label"
        printf '%s\n' "$hits" | sed 's/^/    /' | head -20
        fail=1
    fi
}

# 192.168.1.x is the conventional documentation example; anything else in
# 192.168/16 is treated as a real deployment address and must not be published.
scan "内网地址" '(^|[^0-9])192\.168\.([2-9]|[1-9][0-9]+)\.[0-9]+'
scan "私钥" 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'
scan "证书" 'BEGIN CERTIFICATE'
scan "口令字段" '(password|passwd|secret|token)[[:space:]]*=[[:space:]]*["'\''][^"'\''[:space:]]{6,}'
scan "SSH 助手" 'sshpass|SSHPASS'
scan "个人路径" '/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+'
scan "本地凭据文件" 'lxc-admin-access|ps5-manager-access'
scan "无线网络名" '(SSID|ssid)[[:space:]]*=[[:space:]]*["'\''][^"'\'']+'

if [ "$fail" -eq 0 ]; then
    echo "未发现隐私信息，可以提交。"
else
    echo "发现可疑内容，请先清理再提交。" >&2
    exit 1
fi
