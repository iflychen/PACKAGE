#!/usr/bin/env bash
# =============================================================================
#  把上游 image 鎖定成 digest
#  ---------------------------------------------------------------------------
#  問題：postgres:18 / ollama/ollama:latest / open-webui:main 都是「浮動標籤」。
#       上游一更新，下次 docker compose pull 就換了一個版本。
#       open-webui 尤其危險 —— openwebui_bootstrap.py 直接操作它的 webui.db，
#       上游改 schema 就會在某次重開機時無聲壞掉。
#
#  做法：不去猜哪個版本號是對的，而是把「你現在驗證過、正在跑的這一版」
#       用 digest 釘死寫回 .env。digest 是內容雜湊，永遠指向同一個 image。
#
#  用法：
#      ./scripts/pin-upstream-images.sh          # 鎖定並寫回 .env
#      ./scripts/pin-upstream-images.sh --show   # 只顯示，不修改任何檔案
#
#  解除鎖定：把 .env 那幾行改回 .env.example 的原始值即可。
#
#  相容性：刻意不使用 bash 4 的關聯陣列，macOS 內建的 bash 3.2 也能跑。
# =============================================================================

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

SHOW_ONLY=0
if [ "${1:-}" = "--show" ]; then
    SHOW_ONLY=1
fi

VARS="POSTGRES_IMAGE OLLAMA_IMAGE OPENWEBUI_IMAGE PYTHON_INIT_IMAGE"

default_for() {
    case "$1" in
        POSTGRES_IMAGE)    echo "postgres:18" ;;
        OLLAMA_IMAGE)      echo "ollama/ollama:latest" ;;
        OPENWEBUI_IMAGE)   echo "ghcr.io/open-webui/open-webui:main" ;;
        PYTHON_INIT_IMAGE) echo "python:3.12-slim" ;;
        *)                 echo "" ;;
    esac
}

# .env 有值就用 .env 的，否則用預設值。
current_value() {
    var="$1"
    value=""
    if [ -f "$ENV_FILE" ]; then
        value="$(grep -E "^${var}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    fi
    if [ -z "$value" ]; then
        value="$(default_for "$var")"
    fi
    echo "$value"
}

# 取出 ref 的 repo 部分（去掉 :tag）。ghcr.io/a/b:main -> ghcr.io/a/b
repo_of() {
    ref="$1"
    case "$ref" in
        *:*)
            # 只有最後一段含 : 才算 tag；registry:port/ 這種不算。
            last="${ref##*/}"
            case "$last" in
                *:*) echo "${ref%:*}" ;;
                *)   echo "$ref" ;;
            esac
            ;;
        *) echo "$ref" ;;
    esac
}

# 從本機已下載的 image 解析出 registry digest，回傳 repo@sha256:...
resolve_digest() {
    ref="$1"

    # 已經是 digest 形式就原樣回傳。
    case "$ref" in
        *@sha256:*) echo "$ref"; return 0 ;;
    esac

    if ! docker image inspect "$ref" >/dev/null 2>&1; then
        echo "  本機沒有這個 image，先 pull..." >&2
        docker pull "$ref" >/dev/null 2>&1 || {
            echo "  pull 失敗：$ref" >&2
            return 1
        }
    fi

    repo="$(repo_of "$ref")"

    # RepoDigests 形如 postgres@sha256:...；優先挑 repo 相符的那一筆。
    digest_line="$(docker image inspect "$ref" \
        --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null \
        | grep -E "^${repo}@sha256:" | head -1 || true)"

    if [ -z "$digest_line" ]; then
        digest_line="$(docker image inspect "$ref" \
            --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null \
            | grep -E '@sha256:' | head -1 || true)"
    fi

    if [ -z "$digest_line" ]; then
        echo "  取不到 digest（本機自行 build、未推送到 registry 的 image 沒有 digest）" >&2
        return 1
    fi

    echo "$digest_line" | tr -d '[:space:]'
}

command -v docker >/dev/null 2>&1 || {
    echo "找不到 docker 指令。" >&2
    exit 1
}

echo "=== 解析目前的上游 image digest ==="
echo

RESULT_FILE="$(mktemp)"
trap 'rm -f "$RESULT_FILE"' EXIT
FAILED=0

for var in $VARS; do
    ref="$(current_value "$var")"
    printf '%-20s %s\n' "$var" "$ref"
    if pinned="$(resolve_digest "$ref")"; then
        printf '%-20s → %s\n\n' "" "$pinned"
        printf '%s\t%s\n' "$var" "$pinned" >> "$RESULT_FILE"
    else
        echo
        FAILED=1
    fi
done

if [ "$FAILED" -ne 0 ]; then
    echo "有 image 無法解析。請先把系統跑起來（docker compose up -d）再執行一次。" >&2
    exit 1
fi

if [ "$SHOW_ONLY" -eq 1 ]; then
    echo "（--show 模式，未修改 .env）"
    exit 0
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "找不到 ${ENV_FILE}。請先執行：cp .env.example .env" >&2
    exit 1
fi

BACKUP="${ENV_FILE}.bak.$(date +%Y%m%d%H%M%S)"
cp "$ENV_FILE" "$BACKUP"
echo "已備份原本的 .env → $(basename "$BACKUP")"

while IFS="$(printf '\t')" read -r var value; do
    [ -n "$var" ] || continue
    if grep -qE "^${var}=" "$ENV_FILE"; then
        # 用 | 當 sed 分隔符，因為 image 名稱含 /
        sed -e "s|^${var}=.*|${var}=${value}|" "$ENV_FILE" > "${ENV_FILE}.new"
        mv "${ENV_FILE}.new" "$ENV_FILE"
    else
        printf '%s=%s\n' "$var" "$value" >> "$ENV_FILE"
    fi
done < "$RESULT_FILE"

echo
echo "=== 完成，.env 已鎖定到 digest ==="
grep -E "^(POSTGRES_IMAGE|OLLAMA_IMAGE|OPENWEBUI_IMAGE|PYTHON_INIT_IMAGE)=" "$ENV_FILE"
echo
echo "驗證：docker compose config | grep 'image:'"
