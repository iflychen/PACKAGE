-- ============================================================================
--  Phase I 試算疑似異常點的覆核紀錄
--  --------------------------------------------------------------------------
--  試算覆核      一個疑似異常點一列，記「目前狀態」。試算時由 syncTrialReviews()
--                以 pending 寫入，覆核後由 saveTrialReview() 更新成 reviewed。
--  試算覆核歷程  每次覆核寫一筆，只新增不修改。重新覆核不會蓋掉前一次的判斷。
--
--  排除基準點必須交代理由，這是稽核要求，所以規則寫在 CHECK 約束而不是只放
--  在前端 —— 繞過 UI 直接打 API 也塞不進不合規的資料。
--
--  外鍵一律 ON UPDATE CASCADE ON DELETE RESTRICT，跟其他主表一致：機台或尺寸
--  改名會同步過來；有覆核紀錄的主資料刪不掉，稽核紀錄不會被連帶刪除。
--
--  取代 20260904_phase_i_trial_review.sql 的 phase_i_trial_review(英文欄位、
--  沒有外鍵、不分區間用 -1 哨兵值)。舊表若存在，資料會搬過來，舊表改名成
--  phase_i_trial_review_legacy 保留。
--
--  這個檔案是冪等的，database-init 每次啟動都會重跑一次。
-- ============================================================================

CREATE TABLE IF NOT EXISTS "試算覆核" (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  "品號"            varchar NOT NULL,
  "製程"            varchar NOT NULL,
  "機台"            varchar NOT NULL,
  "球標尺寸名"      varchar NOT NULL,
  "管制圖類型"      varchar NOT NULL,
  -- NULL = 不分事件區間。唯一約束用 NULLS NOT DISTINCT，NULL 之間也會互相衝突。
  "事件紀錄id"      integer,
  -- I-MR 是該工件的流水號；Xbar-R / Xbar-S 是該子組第一筆的流水號。
  "流水號"          integer NOT NULL,
  -- 上圖有觸發時是量測值；只在下圖觸發時是 MR / R / S 的值。
  "實際值"          numeric NOT NULL,
  -- 這個點在「最近一次試算」是否仍被判為異常。重算後不再異常的點會設成
  -- false，但保留該列，因為人工覆核的理由本身就是要留存的稽核紀錄。
  "試算是否異常"    boolean NOT NULL DEFAULT true,
  "違反規則"        jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- 同一個點可能同時在上圖與下圖觸發，合併成 [{chart, component_type}]
  "訊號來源"        jsonb NOT NULL DEFAULT '[]'::jsonb,
  "覆核狀態"        varchar NOT NULL DEFAULT 'pending',
  "基準處置"        varchar,
  "排除原因"        varchar,
  "排除說明"        text,
  "覆核人員"        varchar,
  "覆核時間"        timestamptz,
  "更新時間"        timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,

  CONSTRAINT "試算覆核_選取鍵" UNIQUE NULLS NOT DISTINCT (
    "品號", "製程", "機台", "球標尺寸名", "管制圖類型", "事件紀錄id", "流水號"
  ),
  FOREIGN KEY ("品號", "製程", "球標尺寸名")
    REFERENCES "球標尺寸" ("品號", "製程", "球標尺寸名")
    ON UPDATE CASCADE ON DELETE RESTRICT,
  -- 工件 的 PK 是 (機台, 流水號)，機台 的外鍵也一併由這條涵蓋。
  FOREIGN KEY ("機台", "流水號")
    REFERENCES "工件" ("機台", "流水號")
    ON UPDATE CASCADE ON DELETE RESTRICT,
  FOREIGN KEY ("事件紀錄id")
    REFERENCES "事件紀錄" (id)
    ON UPDATE CASCADE ON DELETE RESTRICT,

  CHECK ("管制圖類型" IN ('I-MR', 'Xbar-R', 'Xbar-S')),
  CHECK ("覆核狀態" IN ('pending', 'reviewed')),
  CHECK ("基準處置" IS NULL OR "基準處置" IN ('retain', 'exclude')),
  CHECK (
    "排除原因" IS NULL OR "排除原因" IN (
      'tool_issue', 'machine_adjustment', 'measurement_error', 'material_issue',
      'fixture_positioning', 'startup_first_piece', 'operator_error', 'other'
    )
  ),
  -- pending 不可有處置；exclude 必須有原因；原因為 other 時必須有補充說明。
  CHECK (
    ("覆核狀態" = 'pending' AND "基準處置" IS NULL)
    OR ("覆核狀態" = 'reviewed' AND "基準處置" = 'retain')
    OR (
      "覆核狀態" = 'reviewed'
      AND "基準處置" = 'exclude'
      AND "排除原因" IS NOT NULL
      AND ("排除原因" <> 'other' OR NULLIF(BTRIM("排除說明"), '') IS NOT NULL)
    )
  )
);

CREATE TABLE IF NOT EXISTS "試算覆核歷程" (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  "覆核id"          bigint NOT NULL
    REFERENCES "試算覆核" (id) ON UPDATE CASCADE ON DELETE RESTRICT,
  "基準處置"        varchar NOT NULL,
  "排除原因"        varchar,
  "排除說明"        text,
  "覆核人員"        varchar,
  "覆核時間"        timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,

  CHECK ("基準處置" IN ('retain', 'exclude')),
  CHECK (
    "排除原因" IS NULL OR "排除原因" IN (
      'tool_issue', 'machine_adjustment', 'measurement_error', 'material_issue',
      'fixture_positioning', 'startup_first_piece', 'operator_error', 'other'
    )
  ),
  CHECK (
    "基準處置" = 'retain'
    OR (
      "排除原因" IS NOT NULL
      AND ("排除原因" <> 'other' OR NULLIF(BTRIM("排除說明"), '') IS NOT NULL)
    )
  )
);

CREATE INDEX IF NOT EXISTS "試算覆核歷程_覆核id_idx"
  ON "試算覆核歷程" ("覆核id", "覆核時間");

-- ----------------------------------------------------------------------------
--  從舊表 phase_i_trial_review 搬資料。新環境沒有舊表，這段是 no-op。
--
--  整段在同一個 DO 區塊裡，任何一列搬不過來(point_id 不是數字、對不到工件
--  或球標尺寸)就整段回滾，舊表維持原樣，database-init 失敗並印出原因。
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  IF to_regclass('public.phase_i_trial_review') IS NULL THEN
    RETURN;
  END IF;

  IF to_regclass('public.phase_i_trial_review_legacy') IS NOT NULL THEN
    RAISE EXCEPTION
      'phase_i_trial_review 與 phase_i_trial_review_legacy 同時存在，請人工確認後刪除其中一張';
  END IF;

  -- 更早期的版本欄位叫 tool_interval_key。
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'phase_i_trial_review'
       AND column_name = 'tool_interval_key'
  ) THEN
    ALTER TABLE phase_i_trial_review
      RENAME COLUMN tool_interval_key TO event_interval_key;
  END IF;

  INSERT INTO "試算覆核" (
    "品號", "製程", "機台", "球標尺寸名", "管制圖類型", "事件紀錄id", "流水號",
    "實際值", "試算是否異常", "違反規則", "訊號來源",
    "覆核狀態", "基準處置", "排除原因", "排除說明", "覆核人員", "覆核時間",
    "更新時間"
  )
  SELECT
    product, process, machine, feature_name, chart_type,
    NULLIF(event_interval_key, -1), point_id::integer,
    actual_value, trial_violation, violated_rules, signal_sources,
    review_status, baseline_disposition, exclusion_reason_code, exclusion_note,
    reviewed_by, reviewed_at, updated_at
  FROM phase_i_trial_review
  ON CONFLICT ON CONSTRAINT "試算覆核_選取鍵" DO NOTHING;

  -- 已覆核的點各補一筆起始歷程。舊表只留了最後一次判斷，更早的已經無從得知。
  INSERT INTO "試算覆核歷程" (
    "覆核id", "基準處置", "排除原因", "排除說明", "覆核人員", "覆核時間"
  )
  SELECT r.id, r."基準處置", r."排除原因", r."排除說明", r."覆核人員",
         COALESCE(r."覆核時間", r."更新時間")
    FROM "試算覆核" r
   WHERE r."覆核狀態" = 'reviewed'
     AND NOT EXISTS (SELECT 1 FROM "試算覆核歷程" h WHERE h."覆核id" = r.id);

  ALTER TABLE phase_i_trial_review RENAME TO phase_i_trial_review_legacy;
END
$$;
