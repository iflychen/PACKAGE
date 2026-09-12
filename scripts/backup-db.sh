#!/usr/bin/env bash
# =============================================================================
#  REALDB 備份
#  ---------------------------------------------------------------------------
#  辨識結果與核准過的管制界線全部只存在 pgdata volume 裡。
#  volume 一掉（docker compose down -v、磁碟壞軌）資料就沒了，所以要定期備份。
#
#  用法：
#      ./scripts/backup-db.sh                 # 備份到 ./backups/
#      ./scripts/backup-db.sh /path/to/dir    # 備份到指定資料夾
#
#  還原：
#      docker compose cp backups/REALDB_2026xxxx.dump postgres:/tmp/r.dump
#      docker compose exec postgres pg_restore -U postgres -d REALDB \
#          --clean --no-owner --no-privileges /tmp/r.dump
#
#  排程（Linux，每天凌晨 3 點）：
#      crontab -e
#      0 3 * * * cd /path/to/ipqc-spc-system && ./scripts/backup-db.sh >> backups/cron.log 2>&1
#
#  排程（Windows）：工作排程器執行
#      wsl -e bash -c "cd /mnt/c/path/to/ipqc-spc-system && ./scripts/backup-db.sh"
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-${REPO_ROOT}/backups}"
# 保留幾份，再舊的自動刪除。
KEEP="${BACKUP_KEEP:-14}"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${OUT_DIR}/REALDB_${STAMP}.dump"

mkdir -p "$OUT_DIR"

cd "$REPO_ROOT"

# 用哪個 compose 檔都可以，postgres 服務定義是一樣的。
COMPOSE_FILE="docker-compose.yml"
[ -f "$COMPOSE_FILE" ] || COMPOSE_FILE="docker-compose.prod.yml"

if ! docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null | grep -qx postgres; then
    echo "postgres 容器沒有在執行，無法備份。" >&2
    exit 1
fi

echo "備份 REALDB -> ${OUT_FILE}"

# -Fc = custom format，還原時可以選擇性還原單張表，也比純 SQL 小。
docker compose -f "$COMPOSE_FILE" exec -T postgres \
    pg_dump -U postgres -d REALDB -Fc > "$OUT_FILE"

SIZE="$(du -h "$OUT_FILE" | cut -f1)"

# pg_dump 失敗時可能產生 0 byte 檔案，要擋掉，否則備份看起來有做其實是空的。
if [ ! -s "$OUT_FILE" ]; then
    echo "備份檔是空的，刪除並回報失敗。" >&2
    rm -f "$OUT_FILE"
    exit 1
fi

echo "完成：${OUT_FILE} (${SIZE})"

# --- 清掉舊備份 ---------------------------------------------------------------
#  依檔名排序即可 —— 檔名帶 YYYYmmdd_HHMMSS，字典序等於時間序，
#  也避開了解析 ls 輸出的各種檔名陷阱。
COUNT="$(find "$OUT_DIR" -maxdepth 1 -type f -name 'REALDB_*.dump' | wc -l | tr -d ' ')"
if [ "$COUNT" -gt "$KEEP" ]; then
    REMOVE=$((COUNT - KEEP))
    echo "保留最新 ${KEEP} 份，刪除最舊的 ${REMOVE} 份"
    find "$OUT_DIR" -maxdepth 1 -type f -name 'REALDB_*.dump' \
        | sort \
        | head -n "$REMOVE" \
        | while IFS= read -r old_file; do
              echo "  刪除 $(basename "$old_file")"
              rm -f "$old_file"
          done
fi
