#!/bin/sh
set -eu

table_count="$(
    psql \
        --host=postgres \
        --username=postgres \
        --dbname=REALDB \
        --tuples-only \
        --no-align \
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
    exit 0
fi

source_table_count="$(
    psql \
        --host=postgres \
        --username=postgres \
        --dbname=REALDB \
        --tuples-only \
        --no-align \
        --command="SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = '來源檔案';"
)"

if [ "$source_table_count" -eq 1 ]; then
    echo "REALDB is already initialized; skipping restore"
    exit 0
fi

echo "REALDB contains tables but the required 來源檔案 table is missing" >&2
exit 1
