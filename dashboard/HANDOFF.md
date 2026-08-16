# SPC Dashboard Handoff

## Current Merge Target

`spc-dashboard` is the active frontend folder.

This folder now keeps the AI summary work from `spc-dashboard` and merges the Xbar / R / S chart display and rebuild fixes from `spc-dashboard2`.

Do not replace `spc-dashboard` wholesale with `spc-dashboard2`. `spc-dashboard` has newer AI summary code and chart scaling behavior that `spc-dashboard2` does not have.

## Main Features Kept

- AI summary button is in `app/page.tsx`.
- Next.js AI proxy route is `app/api/ai-summary/route.ts`.
- Python AI summary endpoint is expected at `SPC_API_BASE/spc/ai-summary`.
- AI summary is not stored in DB. The frontend sends the current chart response as `chartData`, and the backend returns a text summary.

## Phase I Review / Phase II Flow

The dashboard now uses an explicit human-approval workflow:

1. The user selects product, process, machine, feature, and chart type.
2. `GET /api/chart` checks for a currently effective active control-limit version.
3. If an active version exists, the dashboard uses the DB limits directly. It does not calculate trial limits or update the active version.
4. If no active version exists, the dashboard shows `尚未建立管制界線` and a `Phase I 試算` button.
5. `POST /api/control-limit/trial` loads historical measurements and calculates trial limits without writing to DB.
6. Suspected abnormal points are returned by the Python SPC service and shown as exclusion checkboxes.
7. Recalculation sends the selected `excluded_point_ids` back to the same trial endpoint.
8. `POST /api/control-limit/approve` recalculates on the server, inserts the approved control-chart version, and starts Phase II.

Automatic creation is configurable from the password-protected settings dialog:

- Setting key/API field: `auto_create_control_limit`
- Default: `false`
- When enabled, page initialization and a setting change run a sequential batch for
  every feature under the current product / process / machine / chart type / tool
  interval. Existing active versions are skipped by the server-side full-key check.
- A trial is automatically approved only when all of these are true:
  - it is the initial trial request;
  - the automatic setting is enabled;
  - no point has been excluded;
  - neither the primary nor secondary trial chart contains a suspected abnormal point.
- Recalculation after a user selects exclusions never auto-approves. The user
  must inspect the new trial chart and press `核准並進入 Phase II`.
- The auto-approval decision is enforced in
  `app/api/control-limit/trial/route.ts`, not trusted to frontend state.
- The batch reports processed / total progress, keeps insufficient or suspicious
  features pending for manual review, then refreshes processes, tool intervals,
  chart data, capability data, and derived abnormal statistics.
- Selection changes (product, process, machine, tool interval, chart type, or
  feature row) must not trigger another batch. Automatic batches run only once on
  initial page load when enabled, when the setting changes from off to on, or when
  the user presses the explicit recheck button. The selected tool interval is a
  shared table-level filter and is preserved while changing features.
- Every queued batch carries one explicit trigger reason (`initial`, `enabled`, or
  `manual`) which is consumed before the request starts. Any user selection change
  cancels an initial batch that is still waiting for tool-interval initialization;
  React Strict Mode bootstrap is guarded so it cannot queue the initial batch twice.
- Batch problems are also marked in the section ② capability table: orange for
  suspected points/manual review, yellow for insufficient samples, and red for a
  calculation/request failure. A DB-confirmed active feature clears its old marker.

Important behavior:

- Page load never calls the legacy bulk rebuild endpoint. When automatic creation is
  enabled it uses the same trial / approve workflow feature-by-feature instead.
- Measurement ingest/notify never creates or recalculates control limits during Phase I.
- Phase II measurement processing only analyzes against the active DB limits.
- Approval is the only normal UI path that writes a new active control-limit version.
- The server recalculates again during approval so client-supplied limit values are never trusted.

## Abnormal List and Multi-point Rules

- The right-side abnormal list is separated into:
  - top chart: I or Xbar
  - bottom chart: MR, R, or S
- Both chart sections are always visible, including a `0 筆異常` state. The top
  section uses a blue card/tag and the bottom section uses a purple card/tag;
  every abnormal row also repeats its chart source label.
- Top-chart points can be classified as out of specification or out of control.
- Bottom-chart points only use their secondary control limits and are classified as out of control.
- Rule scope is intentionally different by chart:
  - I and Xbar: UCL/LCL plus Western 2-of-3, Western 4-of-5, and Nelson eight-point rules.
  - MR, R, and S: UCL/LCL point checks only; no Western or Nelson run rules.
- The summary cards are explicitly labeled as top-chart statistics to avoid mixing two chart populations.
- Window-based SPC rules now mark every contributing point instead of only the final point that completes the window:
  - Nelson eight points on the same side marks all eight points.
  - Western two-of-three marks the qualifying points beyond 2σ.
  - Western four-of-five marks the qualifying points beyond 1σ.
- This behavior is implemented in `RealProject-main/app/spc.py`; both primary and secondary chart series inherit it through `detect_control_rule_violations`.

New implementation files:

- `app/api/control-limit/trial/route.ts`
- `app/api/control-limit/approve/route.ts`
- `app/api/config/route.ts`
- `lib/controlLimitWorkflow.ts`
- `lib/config.ts`

The existing `app/api/rebuild-all-control-limits/route.ts` is retained as a legacy/manual maintenance endpoint, but the dashboard does not call it.

## Single-Window Dashboard Layout

`app/page.tsx` and `app/globals.css` were rewritten so the whole dashboard fits
one `100vh` screen with no page scrolling. Content is grouped into five coloured
outer containers; every child sits on a white card inside its container:

| Container | Colour | Contents |
| --- | --- | --- |
| ① 選擇 | gray | product / process / machine / tool / chart-type selects, threshold readout, settings gear |
| ② 製程能力 | blue | CPK table (all features), two capability metric rows, daily X-bar trend |
| ③ 管制監控 | green | primary chart and secondary chart side by side, monitor/analysis toggle |
| ④ 異常判定 | amber | verdict counts for the primary chart, split abnormal list |
| ⑤ 決策 | purple | Phase I trial / approve, AI summary |

Layout rules to preserve:

- `html, body { overflow: hidden }` and `.dash { height: 100vh }`. Do not
  reintroduce page scrolling; only inner lists scroll.
- The CPK table replaces the old 球標尺寸 dropdown. Clicking a row sets the
  selected feature and re-drives the metrics, trend, and both charts.
- `ControlChart` and `MovingRangeChart` take an optional `height` prop so the
  same components work in the compact grid. Default heights are unchanged.
- The primary and secondary charts are two separate cards, not stacked in one.

## Process Capability Panel

- `GET /api/capability?product=&process=&machine=&feature=` returns
  `table` (per-feature Cpk), `metrics` (selected feature detail), `daily`
  (per-day X-bar points, day count, last measured timestamp), and the current
  `cpk_threshold`.
- Python only exposes single-feature `POST /spc/capability`, so the route calls
  it once per feature with a concurrency cap of 6. A single feature failing does
  not fail the request; only an all-features failure returns 502.
- `calculateCapabilityDetail()` in `lib/spcClient.ts` passes
  `target_value = 定義值` explicitly. Without it Python defaults the target to
  `(USL + LSL) / 2`, which is wrong for asymmetric tolerances and skews
  Cpm / Cpmk.
- Daily aggregation is pure SQL in `lib/db.ts` (`getDailySummary`). Do not route
  it through `build-chart-data`: per-day sample counts differ and
  `_normalize_subgroups()` rejects unequal subgroups.
- `cpk_threshold` is a password-protected runtime setting (default 1.33,
  range 0.5–3). It drives both the 良品 / 不良 verdict and the four-band table
  colouring.

### Which samples feed which number

`測量值.是否異常` is set by the ingest/notify analysis. Three different sample
sets are in play and mixing them up produces silently wrong limits:

| Consumer | Sample set |
| --- | --- |
| Control chart plot | **all** points minus user-excluded ones — abnormal points must stay visible |
| Trial control limits | first N **clean** points (`是否異常 = FALSE`) |
| Cp / Cpk / Cpm / Cpmk / Ppk | **all clean** points in the interval |
| `getIntervalSampleStats` (sample count, 管制開始時間) | **all clean** points |

`getFeature()` returns `is_abnormal` per measurement rather than filtering in
SQL, precisely because the chart and the baseline need different sets. Do not
add a `是否異常` filter to that query — it would erase abnormal points from the
chart.

Xbar / S charts filter abnormal points *before* forming subgroups. Dropping
individual points mid-stream would shift every subgroup boundary; excluding the
affected data before grouping matches the usual practice of discarding whole
subgroups.

### Two different time bases in one 管制圖 row

A single row mixes a frozen baseline with a rolling metric:

- `管制上界一` / `管制中線一` / `管制下界一` and the 二 set — computed from the
  **first N clean samples** of the interval, frozen at approval. These never
  change again.
- `cp` / `cpk` / `cpm` / `cpmk` / `ppk` — computed from **all clean samples in
  the interval**, and rewritten on **every dashboard load** by
  `/api/capability`. The stored value therefore reflects the last time someone
  opened the page, not the approval moment.

This is intentional (limits must be stable, capability must be current) but it
is not obvious from the schema. If you ever need a true approval-time snapshot
of capability, add separate columns rather than repurposing these.

### Capability write-back

After recalculating, `/api/capability` writes `cp` / `cpk` / `cpm` / `cpmk` /
`ppk` back to the currently effective `管制圖` row via
`updateCapabilityValues()` in `lib/db.ts`. This keeps the DB columns in sync
with what the dashboard shows instead of freezing them at approval time.

Rules:

- Only the capability columns are written. The six control-limit columns are
  never touched here — limits may only be created through the approval flow.
- The target row is matched with the same active-version predicate as
  `getFeature()` (`管制是否啟用 = TRUE` plus the start/end time window).
- Features still in Phase I have no active row, so they are counted as
  `skipped`, not as an error.
- A write failure never fails the request; it is reported in
  `persisted.errors` and surfaced in the ② box header.
- `approveFeatureTrial()` and `/api/capability` both call
  `calculateCapabilityDetail()`, which passes `target_value = 定義值`. Do not
  switch either back to `calculateCapability()` — the values would diverge for
  asymmetric tolerances.

## Event Intervals (formerly Tool Intervals)

The tool-change tables were generalised into event tables. `換刀` is now just
one value of a free-text `事件類型` column, alongside `保養`, `參數調整`, etc.

Schema was verified against `information_schema` on the live Neon branch.

| Object | Kind | Key / columns |
| --- | --- | --- |
| `事件紀錄` | table | PK(`id`) + `機台`, `起始流水號`, `事件時間`, `事件類型`, `備註` — **no end serial** |
| `事件使用區間` | **view** | `事件紀錄id`, `機台`, `事件類型`, `起始流水號`, `結束流水號_不含`, `事件時間` |
| `工件_含事件` | **view** | all of `工件` + `事件類型`, `事件紀錄id`, `事件啟用時間` |

Renamed from `換刀紀錄` / `刀具使用區間` / `工件_含刀具`; `換刀時間` became
`事件時間` and `刀具啟用時間` became `事件啟用時間`.

The two views do the interval maths already — `事件使用區間` derives the
exclusive upper bound with `LEAD()`, and `工件_含事件` pre-assigns every 工件 to
its interval. So:

- listing intervals for a machine → read `事件使用區間`;
- scoping measurements to one interval → `JOIN "工件_含事件" w ON w.機台 = m.機台
  AND w.流水號 = m.流水號` then compare `w."事件紀錄id"`.

Do **not** hand-roll the serial-range join; it duplicates view logic and is
easy to get wrong at the open-ended last interval.

### 事件類型 is a correctness condition, not an optional filter

`工件_含事件` emits **one row per (工件 × 事件類型)**. That is not duplicated
data — it is the same 工件 seen from each event timeline. Consequences:

- Every `JOIN "工件_含事件"` **must** carry
  `AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))`.
  Omit it and every measurement is multiplied by the number of event types.
  Cpk shifts, sample counts inflate, and `管制開始時間` lands on an earlier
  row — **all silently, with no error**.
- The condition belongs in the `ON` clause, not `WHERE`. On the two `LEFT JOIN`
  sites (`getFeature` measurements, `listFeatureSpecsWithValues`) a `WHERE`
  predicate would degrade them to inner joins and drop 工件 that have no event
  record at all.
- `事件使用區間` must also be filtered by 事件類型: its `LEAD()` runs *within*
  one event type, so an unfiltered read interleaves 換刀 and 保養 intervals.

`lib/db.ts` currently has 8 such joins. If you add a 9th, add the condition.

### Event type plumbing

- `DEFAULT_EVENT_TYPE = "換刀"` lives in `lib/types.ts`. Every db function takes
  `eventType` as a defaulted trailing parameter, so legacy callers
  (`lib/measurementProcessor.ts`, `rebuild-all-control-limits`) keep the
  pre-generalisation behaviour without changes.
- `GET /api/event-types?machine=` lists the 事件類型 values actually present in
  `事件紀錄`. The dropdown must be populated from DB — 事件類型 is free text and
  cannot be hardcoded. The route returns `default_event_type`, which is `換刀`
  only when that value really exists for the machine.
- `GET /api/tool-intervals` is now `GET /api/event-intervals` and takes an
  `event_type` parameter. `/api/chart`, `/api/capability`,
  `/api/control-limit/trial` and `/api/control-limit/approve` all take
  `event_type` too.
- Changing 事件類型 in the UI resets `selInterval` to null. Interval ids are
  scoped to one event type; reusing an id across types selects zero rows.
- Like the other selects, changing 事件類型 cancels a pending auto batch and
  must never trigger a new one.

### Known limitation: 管制圖 has no 事件類型 column

Control-limit versions are matched to an interval purely through
`管制開始時間`. If control limits were ever approved for two different event
types whose intervals cover the same measurements, both would compute the same
`管制開始時間` and overwrite each other's row through the `ON CONFLICT` upsert.

Today only `換刀` produces control limits, so this cannot happen. Before
approving limits under a second event type, add `事件類型` to `管制圖` and
include it in the primary key.

Intervals are keyed by machine only, so one tool can span several products.
`流水號` is unique per machine (`工件` PK is `(機台, 流水號)`), not per product.
Control limits are still computed per
`(品號, 製程, 機台, 球標尺寸名, 管制圖類型, 區間)`.

### Joining 工件 and 測量值

`工件` PK is `(機台, 流水號)` and `測量值` PK is `(機台, 流水號, 球標尺寸名)` —
neither includes 品號 / 製程. Join on `(機台, 流水號)` only. Adding 品號 / 製程 to
the join condition silently drops rows whenever the two tables disagree on those
columns (they are separate FK paths: 測量值 → 球標尺寸, 工件 → 製程).

### Matching a control-chart version to an interval

`管制圖` has no interval column, so the link is `管制開始時間`. Since that value
is the Nth sample's measurement time, it always falls inside the interval's own
measurement window. `getFeature()` therefore scopes the version lookup with:

```sql
c."管制開始時間" BETWEEN (interval min 量測時間) AND (interval max 量測時間)
```

Without this the query returns the newest version regardless of the selected
interval, silently plotting old-tool points against new-tool limits.

**Every interval keeps its own active row.** `approveFeatureTrial()` only
deactivates older versions when `tool_interval_id` is null (machines with no
tool records). Deactivating them in interval mode would make older tools fall
back to Phase I when reselected.

### 管制開始時間

Defined as **the measurement time of the Nth clean sample inside the
interval**, where N is `min_samples`. That is the moment enough data had
accumulated to compute limits, so Phase II starts there — not at the tool-change
time and not at approval time.

- `getIntervalSampleStats()` in `lib/db.ts` computes it with
  `ROW_NUMBER()` partitioned by interval plus
  `MIN(measured_at) FILTER (WHERE rn = min_samples)`.
- `GET /api/tool-intervals` returns it per interval so the dropdown can show
  `✓` for ready intervals and `(n/N)` for ones still accumulating.
- `approveFeatureTrial()` writes it into `管制圖.管制開始時間`. It only falls
  back to `now()` when the baseline samples have no 量測時間.
- `管制開始時間` is `NOT NULL` **and part of the `管制圖` primary key**
  (`品號, 製程, 機台, 球標尺寸名, 管制圖類型, 管制開始時間`). The value is
  deterministic per interval, so re-approving would collide — the insert uses
  `ON CONFLICT (...) DO UPDATE` to refresh limits and capability values in
  place. Never write NULL here; the `?? now()` fallback exists for that reason.

### Baseline is frozen

Limits are computed from the **first N samples** of the interval only
(`measurements.slice(0, minSamples)`, or the first `MIN_SUBGROUPS` subgroups for
Xbar charts). Later points are plotted against those fixed limits — that is what
makes it Phase II monitoring rather than a moving average.

Capability values (`cp` … `ppk`) still use *all* samples in the interval, so the
dashboard and the DB agree on current process performance.

## Settings Contract

`GET /api/config` returns all three settings; `POST /api/config` updates any
subset, so callers may send only the field they changed:

| Field | Type | Default |
| --- | --- | --- |
| `min_samples` | number | 5 |
| `cpk_threshold` | number | 1.33 |
| `auto_create_control_limit` | boolean | false |

## DB Contract for Control-Limit Versions

The approval flow expects `管制圖` to support multiple versions with:

- `管制開始時間`
- `管制結束時間`
- `管制是否啟用`
- lowercase capability columns `cp`, `cpk`, `cpm`, `cpmk`, `ppk`

An active version is selected when:

```sql
管制是否啟用 = TRUE
AND (管制開始時間 IS NULL OR 管制開始時間 <= CURRENT_TIMESTAMP)
AND (管制結束時間 IS NULL OR 管制結束時間 > CURRENT_TIMESTAMP)
```

Approved versions store both the primary and secondary chart limits plus the five capability values. `serial_no` / `流水號` is an integer through the Next.js API, DB mapping, and Python SPC request contract. Chart X-axis labels remain strings for display.

## Main Merge Changes

- `app/api/rebuild-all-control-limits/route.ts` now rebuilds three chart types:
  - `I-MR`
  - `Xbar-R`
  - `Xbar-S`
- For `I-MR`, the first control-limit columns are the I chart limits and the second columns are MR chart limits.
- For `Xbar-R`, the first control-limit columns are Xbar chart limits and the second columns are R chart limits.
- For `Xbar-S`, the first control-limit columns are Xbar chart limits and the second columns are S chart limits.
- `lib/db.ts` normalizes chart type comparison with `NORMALIZE(TRIM(...))`, which avoids missing rows when DB values contain extra spaces or normalization differences.
- `app/page.tsx` now shows the correct bottom chart title:
  - `I-MR`: MR chart
  - `Xbar-R`: R chart
  - `Xbar-S`: S chart

## Xbar-R / Xbar-S Assumptions

- Subgroup helper is `lib/subgroups.ts`.
- Default subgroup size is `5`.
- Minimum subgroup count is `3`.
- Therefore Xbar-R and Xbar-S need at least `15` clean measurements before control limits can be generated.
- Clean measurements means rows where `是否異常` is not `TRUE`.

## Important Files

- `app/page.tsx`
  - Main frontend page.
  - Keeps AI summary button and display.
  - Uses chart type-specific titles for top and bottom charts.
- `components/ControlChart.tsx`
  - Keep this version. It includes the newer Y-axis behavior that prevents very wide spec limits from flattening the chart.
  - Supports monitor/analysis display modes. Monitor mode uses all data for a fixed Y-axis. Analysis mode enables independent X brush and Y-axis zoom.
- `components/MovingRangeChart.tsx`
  - Used for MR, R, and S bottom charts.
  - Supports its own independent monitor/analysis mode, X brush, and Y-axis zoom. Do not force it to share the same scale as the top chart.
- `app/api/chart/route.ts`
  - Keep this version. It uses `secondary_chart` from the backend when available.
- `app/api/rebuild-all-control-limits/route.ts`
  - Rebuilds control limits for I-MR, Xbar-R, and Xbar-S.
- `app/api/ai-summary/route.ts`
  - Proxies AI summary requests to the Python backend.
- `lib/spcClient.ts`
  - Handles chart build requests, trial limit calculation, and secondary chart conversion.
- `lib/subgroups.ts`
  - Splits measurement rows into Xbar subgroups.
- `lib/db.ts`
  - Reads feature/spec/measurement/control-limit data from Neon.

## Backend Dependencies

The frontend expects `RealProject-main` to expose:

- `POST /spc/build-chart-data`
- `POST /spc/calculate-trial-limits`
- `POST /spc/ai-summary`

AI summary backend files:

- `RealProject-main/app/ai_summary.py`
- `RealProject-main/app/llm.py`

Ollama model expected by `llm.py`:

- `qwen3.5:9b`

## Local Verification

From `spc-dashboard`:

```bash
npm run typecheck
npm run build
npm run dev
```

Backend should be running separately, usually:

```bash
uvicorn app.main:app --reload
```

Make sure `SPC_API_BASE` points to the Python backend. If unset, frontend defaults to:

```bash
http://127.0.0.1:8000
```

## Known Notes

- `spc-dashboard2` was used only as a source for Xbar-R / Xbar-S display and rebuild behavior.
- The current merge target remains `spc-dashboard`.
- Rebuild-all-control-limits no longer runs on page load. Automatic activation, when
  enabled, must stay on the guarded trial / approve workflow and must never use the
  legacy rebuild endpoint; suspicious trials still require human review.
- AI summary only summarizes the current chart response and should not invent machine, shift, or operator details that are not present in the data.
- Top and bottom charts should keep independent zoom state and independent Y-axis scale. This is important because Xbar values and R/S/MR values are different units/ranges.
- Monitor mode should remain visually stable for long-term comparison. Analysis mode can rescale X/Y to inspect small shifts, trends, and cyclic movement.
