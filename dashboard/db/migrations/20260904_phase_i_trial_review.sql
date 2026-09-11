CREATE TABLE IF NOT EXISTS phase_i_trial_review (
  product                 varchar NOT NULL,
  process                 varchar NOT NULL,
  machine                 varchar NOT NULL,
  feature_name            varchar NOT NULL,
  chart_type              varchar NOT NULL,
  tool_interval_key       integer NOT NULL DEFAULT -1,
  point_id                varchar NOT NULL,
  actual_value            numeric NOT NULL,
  trial_violation         boolean NOT NULL DEFAULT true,
  violated_rules          jsonb NOT NULL DEFAULT '[]'::jsonb,
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
    tool_interval_key, point_id
  ),
  CHECK (review_status IN ('pending', 'reviewed')),
  CHECK (baseline_disposition IS NULL OR baseline_disposition IN ('retain', 'exclude')),
  CHECK (
    exclusion_reason_code IS NULL OR exclusion_reason_code IN (
      'tool_issue', 'machine_adjustment', 'measurement_error', 'material_issue',
      'fixture_positioning', 'startup_first_piece', 'operator_error', 'other'
    )
  ),
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
    product, process, machine, feature_name, chart_type, tool_interval_key
  );
