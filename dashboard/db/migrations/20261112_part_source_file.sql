-- ============================================================================
--  工件 → 來源檔案
--  --------------------------------------------------------------------------
--  一份檢查表 PDF 經 pipelines 辨識後只會產生一筆工件(metadata 只有一個
--  流水號)，該工件的測量值都來自這份檔案。在 工件 記下來源檔名，量測值就
--  能一路追回原始檢查表。
--
--  可為 NULL：dump / 示範資料 / dashboard 的 /api/measurements/ingest 寫入
--  的工件都沒有對應檔案。
--
--  同一個工件被重新匯入時，pipelines 會把 來源檔名 更新成最新那份檔案。
--
--  這個檔案是冪等的，database-init 每次啟動都會重跑一次。
-- ============================================================================

ALTER TABLE "工件" ADD COLUMN IF NOT EXISTS "來源檔名" varchar;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = '"工件"'::regclass
       AND conname = '工件_來源檔名_fkey'
  ) THEN
    ALTER TABLE "工件"
      ADD CONSTRAINT "工件_來源檔名_fkey"
      FOREIGN KEY ("來源檔名") REFERENCES "來源檔案" ("檔名")
      ON UPDATE CASCADE ON DELETE RESTRICT;
  END IF;
END
$$;

-- 刪除 / 改名 來源檔案 時要查哪些工件引用它，沒有索引就是全表掃描。
CREATE INDEX IF NOT EXISTS "工件_來源檔名_idx" ON "工件" ("來源檔名");

-- ----------------------------------------------------------------------------
--  回填：既有 來源檔案 的 檔案內容 是 pipelines 的辨識結果 JSON，
--  metadata 裡有 品號 / 製程 / 機台 / 流水號，可以精確對回工件。
--
--  不是合法 JSON 的內容直接略過(pg_input_is_valid，PostgreSQL 16+)，
--  轉型放在 CASE 裡，避免 planner 先轉型再過濾而整句失敗。
--  同一個工件對到多份檔案時取 上傳時間 最新的，和 pipelines 重新匯入的
--  行為一致。只填 NULL 的列，不覆蓋已有的值。
-- ----------------------------------------------------------------------------
WITH parsed AS (
  SELECT
    "檔名",
    "上傳時間",
    CASE WHEN pg_input_is_valid("檔案內容", 'jsonb')
         THEN "檔案內容"::jsonb -> 'metadata'
    END AS meta
  FROM "來源檔案"
),
matched AS (
  SELECT DISTINCT ON (w."機台", w."流水號")
    w."機台", w."流水號", p."檔名"
  FROM parsed p
  JOIN "工件" w
    ON w."機台"         = p.meta ->> '機台'
   AND w."流水號"::text = p.meta ->> '流水號'
   AND w."品號"         = p.meta ->> '品號'
   AND w."製程"         = p.meta ->> '製程'
  WHERE p.meta IS NOT NULL
  ORDER BY w."機台", w."流水號", p."上傳時間" DESC NULLS LAST
)
UPDATE "工件" w
   SET "來源檔名" = m."檔名"
  FROM matched m
 WHERE w."機台" = m."機台"
   AND w."流水號" = m."流水號"
   AND w."來源檔名" IS NULL;
