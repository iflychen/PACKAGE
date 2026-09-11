#!/bin/sh
set -eu

# ============================================================================
#  REALDB 初始化
#  --------------------------------------------------------------------------
#  1. 資料庫是空的      → 還原 REALDB_backup.dump
#  2. 已有資料且結構正確 → 跳過還原
#  3. 有表但缺「來源檔案」→ 視為異常，中止
#
#  不論走哪一條，最後都會套用 /opt/migrations 底下的 schema 變更腳本。
#
#  ⚠️ 原本版本在還原完與「已初始化」兩處都直接 exit 0，導致任何加在檔案
#     尾端的步驟都執行不到。改成 if/else 收斂，流程一定會走到 migrations。
# ============================================================================

PSQL="psql --host=postgres --username=postgres --dbname=REALDB"

table_count="$(
    $PSQL --tuples-only --no-align \
        --command="SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public';"
)"

if [ "$table_count" -eq 0 ]; then
    echo "REALDB is empty; restoring /backup/REALDB_backup.dump"
    pg_restore \
        --host=postgres \
        --username=postgres \
        --dbname=REALDB \
        --no-owner \
        --no-privileges \
        --exit-on-error \
        /backup/REALDB_backup.dump
    echo "REALDB restore completed"
else
    source_table_count="$(
        $PSQL --tuples-only --no-align \
            --command="SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = '來源檔案';"
    )"

    if [ "$source_table_count" -ne 1 ]; then
        echo "REALDB contains tables but the required 來源檔案 table is missing" >&2
        exit 1
    fi

    echo "REALDB is already initialized; skipping restore"
fi

# ----------------------------------------------------------------------------
#  Schema 變更腳本
#  --------------------------------------------------------------------------
#  來源是 dashboard/db/migrations（compose 以唯讀掛載到 /opt/migrations）。
#  每次啟動都會全部重跑一次 —— 腳本本身必須是冪等的
#  （CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / DO $$ ... $$
#   包條件判斷）。新增 migration 時請維持這個原則，這裡沒有版本追蹤表。
#
#  ON_ERROR_STOP=1 讓任何一行 SQL 失敗就整個中止，避免 schema 半套。
# ----------------------------------------------------------------------------
if [ -d /opt/migrations ]; then
    for migration in /opt/migrations/*.sql; do
        [ -e "$migration" ] || continue
        echo "applying migration $(basename "$migration")"
        $PSQL --set=ON_ERROR_STOP=1 --file="$migration"
    done
    echo "migrations completed"
else
    echo "no /opt/migrations directory mounted; skipping migrations"
fi
