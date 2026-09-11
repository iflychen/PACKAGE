-- ============================================================================
--  Phase I 試算疑似異常點的覆核紀錄
--  --------------------------------------------------------------------------
--  一個疑似異常點一列。試算時由 syncTrialReviews() 以 pending 狀態寫入，
--  使用者覆核後由 saveTrialReview() 更新成 reviewed。
--
--  排除基準點必須交代理由，這是稽核要求，所以規則寫在 CHECK 約束而不是只放
--  在前端 —— 繞過 UI 直接打 API 也塞不進不合規的資料。
--
--  這個檔案是冪等的(IF NOT EXISTS)，database-init 每次啟動都會重跑一次。
-- ============================================================================

CREATE TABLE IF NOT EXISTS phase_i_trial_review (
  product                 varchar NOT NULL,
  process                 varchar NOT NULL,
  machine                 varchar NOT NULL,
  feature_name            varchar NOT NULL,
  chart_type              varchar NOT NULL,
  -- 事件區間 id。主鍵不能有 NULL，所以「不分區間」用 -1 當哨兵值，
  -- 不要改成 nullable。
  event_interval_key      integer NOT NULL DEFAULT -1,
  point_id                varchar NOT NULL,
  actual_value            numeric NOT NULL,
  -- 這個點在「最近一次試算」是否仍被判為異常。重算後不再異常的點會設成
  -- false，但保留該列，因為人工覆核的理由本身就是要留存的稽核紀錄。
  trial_violation         boolean NOT NULL DEFAULT true,
  violated_rules          jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- 同一個 point_id 可能同時在上圖與下圖觸發，合併成 [{chart, component_type}]
  signal_sources          jsonb NOT NULL DEFAULT '[]'::jsonb,
  review_status           varchar NOT NULL DEFAULT 'pending',
  baseline_disposition    varchar,
  exclusion_reason_code   varchar,
  exclusion_note          text,
  reviewed_by             varchar,
  reviewed_at             timestamptz,
  updated_at              timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (
    product, process, machine, feature_name, chart_type,
    event_interval_key, point_id
  ),
  CHECK (review_status IN ('pending', 'reviewed')),
  CHECK (baseline_disposition IS NULL OR baseline_disposition IN ('retain', 'exclude')),
  CHECK (
    exclusion_reason_code IS NULL OR exclusion_reason_code IN (
      'tool_issue', 'machine_adjustment', 'measurement_error', 'material_issue',
      'fixture_positioning', 'startup_first_piece', 'operator_error', 'other'
    )
  ),
  -- pending 不可有處置；exclude 必須有原因；原因為 other 時必須有補充說明。
  CHECK (
    (review_status = 'pending' AND baseline_disposition IS NULL)
    OR (review_status = 'reviewed' AND baseline_disposition = 'retain')
    OR (
      review_status = 'reviewed'
      AND baseline_disposition = 'exclude'
      AND exclusion_reason_code IS NOT NULL
      AND (exclusion_reason_code <> 'other' OR NULLIF(BTRIM(exclusion_note), '') IS NOT NULL)
    )
  )
);

CREATE INDEX IF NOT EXISTS phase_i_trial_review_selection_idx
  ON phase_i_trial_review (
    product, process, machine, feature_name, chart_type, event_interval_key
  );

-- ----------------------------------------------------------------------------
--  相容處理：若資料庫是在改名之前建立的，把舊欄位名換過來。
--  新環境不會有這張舊表，這段是 no-op。
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'phase_i_trial_review'
       AND column_name = 'tool_interval_key'
  ) THEN
    ALTER TABLE phase_i_trial_review
      RENAME COLUMN tool_interval_key TO event_interval_key;
  END IF;
END
$$;
