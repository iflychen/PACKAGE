#!/usr/bin/env bash
# =============================================================================
#  匯出／還原 Ollama 模型（macOS / Linux 版；Windows 請用 ollama-models.ps1）
#  ---------------------------------------------------------------------------
#  模型存在 Docker 具名 volume real-project_ollama_data 裡，不是一般資料夾，
#  所以要掛一個一次性容器進去打包。
#
#  用法：
#      ./scripts/ollama-models.sh export [路徑]
#      ./scripts/ollama-models.sh import [路徑]
#
#  預設路徑：./backups/ollama_models.tar
#
#  什麼時候用得上：重灌 Docker、搬機器、交付到廠內 —— 省下 11 GB 的下載。
# =============================================================================

set -eu

ACTION="${1:-}"
TARGET="${2:-}"
VOLUME="${OLLAMA_VOLUME:-real-project_ollama_data}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -n "$TARGET" ] || TARGET="${REPO_ROOT}/backups/ollama_models.tar"

usage() {
    echo "用法：$0 {export|import} [tar 路徑]" >&2
    exit 2
}

[ "$ACTION" = "export" ] || [ "$ACTION" = "import" ] || usage

command -v docker >/dev/null 2>&1 || {
    echo "找不到 docker 指令。" >&2
    exit 1
}
docker info >/dev/null 2>&1 || {
    echo "Docker 沒有在執行。" >&2
    exit 1
}

DIR="$(cd "$(dirname "$TARGET")" 2>/dev/null && pwd || true)"
if [ -z "$DIR" ]; then
    mkdir -p "$(dirname "$TARGET")"
    DIR="$(cd "$(dirname "$TARGET")" && pwd)"
fi
FILE="$(basename "$TARGET")"

if [ "$ACTION" = "export" ]; then
    if [ -z "$(docker volume ls --quiet --filter "name=^${VOLUME}$")" ]; then
        echo "找不到 volume ${VOLUME}。系統跑起來過嗎？（docker volume ls）" >&2
        exit 1
    fi

    echo "匯出 ${VOLUME} -> ${DIR}/${FILE}"
    echo "11 GB 大約 3-5 分鐘，過程中沒有進度顯示。"

    # 不壓縮：gguf 權重本來就壓過了。
    docker run --rm -v "${VOLUME}:/data" -v "${DIR}:/backup" \
        alpine tar cf "/backup/${FILE}" -C /data .

    SIZE_BYTES="$(wc -c < "${DIR}/${FILE}" | tr -d ' ')"
    if [ "$SIZE_BYTES" -lt 1073741824 ]; then
        echo "警告：檔案小於 1 GB，請確認模型真的下載完成了。" >&2
    fi
    echo "完成：${DIR}/${FILE} ($(du -h "${DIR}/${FILE}" | cut -f1))"
else
    [ -f "${DIR}/${FILE}" ] || {
        echo "找不到 ${DIR}/${FILE}" >&2
        exit 1
    }

    echo "還原 ${DIR}/${FILE} -> ${VOLUME}"
    docker volume create "$VOLUME" >/dev/null

    docker run --rm -v "${VOLUME}:/data" -v "${DIR}:/backup" \
        alpine tar xf "/backup/${FILE}" -C /data

    echo "完成。接著 docker compose up -d，ollama-model-init 會跳過下載。"
    echo "驗證：docker compose exec ollama ollama list"
fi
