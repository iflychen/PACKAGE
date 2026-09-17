// SPC 儀表板首頁 — 單一視窗(100vh 不捲動),五個彩色分組容器:
//   ① 灰 選擇      品號 / 製程 / 機台 / 事件類型 / 事件區間 / 管制圖類型 + 設定
//   ② 藍 製程能力  CPK 統計表 + 能力指標橫條 + 每日 X-bar 折線
//   ③ 綠 管制監控  I chart 與 MR chart 左右並排
//   ④ 橘 異常判定  判定統計 + 異常點清單
//   ⑤ 紫 決策      Phase I 試算/核准 + AI 摘要

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import ControlChart from "@/components/ControlChart";
import MovingRangeChart from "@/components/MovingRangeChart";
import DailyTrendChart from "@/components/DailyTrendChart";
import {
  pointStatus,
  ruleLabel,
  STATUS_COLOR,
  type PointStatus,
} from "@/lib/labels";
import type { ChartApiResponse, ChartType, FeatureCombo } from "@/lib/types";
import { DEFAULT_EVENT_TYPE } from "@/lib/types";
import { EXCLUSION_REASON_OPTIONS } from "@/lib/trialReview";
import type {
  ExclusionReasonCode,
  TrialReviewRecord,
} from "@/lib/trialReview";

const CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];
type ChartViewMode = "monitor" | "analysis";
type MainView = "monitor" | "capability" | "history";

interface ControlLimitVersion {
  product: string;
  process: string;
  machine: string;
  feature_name: string;
  chart_type: ChartType;
  cl: number | null;
  ucl: number | null;
  lcl: number | null;
  mr_cl: number | null;
  mr_ucl: number | null;
  mr_lcl: number | null;
  is_active: boolean;
  started_at: string;
  ended_at: string | null;
  sample_size: number | null;
  cp: number | null;
  cpk: number | null;
  ppk: number | null;
}

function chartTitles(t: ChartType): { top: string; bottom: string } {
  if (t === "Xbar-R") {
    return { top: "Xbar 管制圖 (X-bar chart)", bottom: "全距管制圖 (R chart)" };
  }
  if (t === "Xbar-S") {
    return { top: "Xbar 管制圖 (X-bar chart)", bottom: "標準差管制圖 (S chart)" };
  }
  return {
    top: "個別值管制圖 (I chart)",
    bottom: "移動全距管制圖 (MR chart)",
  };
}

interface ApiError {
  error: string;
  detail?: string;
  spc_api_base?: string;
}

interface PhaseITrialResponse {
  ok: true;
  auto_approved: boolean;
  message?: string;
  sample_count: number;
  subgroup_count: number | null;
  excluded_point_ids: Array<number | string>;
  trial: {
    cl: number;
    ucl: number;
    lcl: number;
    trial_has_abnormal_points: boolean;
    abnormal_points: Array<{
      measurement_id: number | string;
      actual_value: number;
      violated_rules: string[];
    }>;
  };
  // 上圖(超規或失控)+ 下圖(失控)合併後的疑似異常點
  suspected_points: Array<{
    point_id: number | string;
    chart: "primary" | "secondary";
    component_type: string;
    actual_value: number;
    violated_rules: string[];
  }>;
  /**
   * 覆核紀錄。每個 point_id 一筆(上下圖觸發同一點時已合併),
   * 帶著先前存過的處置與原因。覆核表不存在時會是空陣列並附 review_error。
   */
  reviews?: TrialReviewRecord[];
  review_error?: string;
  /** 累積到足夠樣本那一刻的量測時間;核准時會寫進管制圖的「管制開始時間」 */
  control_start_time?: string | null;
  chart: ChartApiResponse;
}

/** 畫面上一列疑似異常點。已依 point_id 合併上下圖來源。 */
interface SuspectedRow {
  pointId: string;
  actualValue: number;
  violatedRules: string[];
  sources: Array<{ chart: "primary" | "secondary"; component_type: string }>;
  /** 先前存過的覆核結果；覆核表不可用時為 null。 */
  review: TrialReviewRecord | null;
}

interface AbnormalListItem {
  x: string;
  value: number;
  status: PointStatus;
  violatedRules: string[];
}

interface AutoTrialItem {
  feature_name: string;
  status: "activated" | "already_active" | "needs_review" | "skipped" | "failed";
  reason: string;
}

/** 事件區間(原換刀區間) —— 依所選事件類型切出來的流水號區段 */
interface EventIntervalOption {
  interval_id: number;
  serial_from: number;
  serial_to_exclusive: number | null;
  changed_at: string | null;
  clean_sample_count: number;
  /** 累積到第 N 筆乾淨樣本的量測時間 = 管制開始時間 */
  control_start_time: string | null;
  first_measured_at: string | null;
  last_measured_at: string | null;
  ready: boolean;
}

interface CapabilityTableRow {
  index: number;
  feature_name: string;
  nominal_value: number;
  upper_tolerance: number;
  lower_tolerance: number;
  usl: number | null;
  lsl: number | null;
  cpk: number | null;
  sample_size: number;
  error?: string;
}

interface CapabilityMetrics {
  feature_name: string;
  cpk: number | null;
  cp: number | null;
  cpm: number | null;
  cpmk: number | null;
  ppk: number | null;
  mean: number | null;
  sigma: number | null;
  usl: number;
  lsl: number;
  nominal_value: number;
  sample_size: number;
}

interface CapabilityResponse {
  ok: true;
  cpk_threshold: number;
  table: CapabilityTableRow[];
  metrics: CapabilityMetrics | null;
  daily: {
    points: Array<{ date: string; value: number; count: number }>;
    day_count: number;
    last_measured_at: string | null;
    /** 折線的聚合粒度 — 資料跨度太短時會自動改成每小時或每筆 */
    bucket?: "day" | "hour" | "point";
  } | null;
  // 能力值回寫「管制圖」的結果
  persisted?: {
    updated: number;
    skipped: number;
    errors: string[];
  };
}

// 從扁平組合清單裡衍生某一層的獨立選項
function uniqueBy<T, K extends string | number>(
  arr: T[],
  keyFn: (x: T) => K,
): T[] {
  const seen = new Set<K>();
  const out: T[] = [];
  for (const x of arr) {
    const k = keyFn(x);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(x);
  }
  return out;
}

/** Cpk 分級配色:以門檻為基準往上下推 */
function cpkColor(cpk: number | null, threshold: number): string {
  if (cpk == null) return "#94a3b8";
  if (cpk >= threshold * 1.25) return "#3b6d11"; // 優良 綠
  if (cpk >= threshold) return "#185fa5"; // 良好 藍
  if (cpk >= threshold * 0.75) return "#854f0b"; // 尚可 黃
  return "#a32d2d"; // 不良 紅
}

function fmt(n: number | null | undefined, digits = 3): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

function fmtTol(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "—";
  return String(Math.abs(n));
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}

/** 事件時間縮寫:MM/DD HH:mm */
function shortStamp(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(5, 16).replace("T", " ").replace("-", "/");
}

/** 折線粒度的中文標題。資料跨度太短時後端會自動降到小時或每筆。 */
function trendLabel(bucket?: "day" | "hour" | "point"): string {
  if (bucket === "hour") return "每小時 X-bar";
  if (bucket === "point") return "每筆量測值";
  return "每日 X-bar";
}

/**
 * 異常清單的「上圖 / 下圖」分區。
 * 上圖(I / XBAR)用藍色系,下圖(MR / R / S)用紫色系;
 * 兩區永遠顯示,沒有異常時顯示 0 筆,避免把兩個圖的族群混在一起看。
 */
function AbnormalSection({
  title,
  chartLabel,
  tone,
  items,
}: {
  title: string;
  chartLabel: string;
  tone: "primary" | "secondary";
  items: AbnormalListItem[];
}) {
  return (
    <div className={`ab-section ${tone}`}>
      <div className="ab-section-head">
        <span className="ab-section-title">{title}</span>
        <span className="ab-section-count">{items.length} 筆</span>
      </div>
      {items.length === 0 ? (
        <div className="ab-empty">無異常點</div>
      ) : (
        <ul className="ab-list">
          {items.map((item, index) => (
            <li
              key={`${tone}-${item.x}-${index}`}
              title={`[${chartLabel}] ${item.violatedRules.map(ruleLabel).join("、")}`}
            >
              <span className="ab-src">{chartLabel}</span>
              <span className="ab-x">#{item.x}</span>
              <span className="ab-v">{item.value}</span>
              <span
                className="ab-t"
                style={{ color: STATUS_COLOR[item.status] }}
              >
                {item.status === "out_of_spec" ? "超規" : "失控"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function Page() {
  const [combos, setCombos] = useState<FeatureCombo[]>([]);

  const [selProduct, setSelProduct] = useState<string>("");
  const [selProcess, setSelProcess] = useState<string>("");
  const [selMachine, setSelMachine] = useState<string>("");
  const [selFeature, setSelFeature] = useState<string>("");
  const [selChartType, setSelChartType] = useState<ChartType>("I-MR");

  // 事件類型(換刀 / 保養 / …)—— 決定區間怎麼切,必須先選才能算區間
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [selEventType, setSelEventType] = useState<string>(DEFAULT_EVENT_TYPE);

  // 事件區間
  const [eventIntervals, setEventIntervals] = useState<EventIntervalOption[]>([]);
  const [selInterval, setSelInterval] = useState<number | null>(null);
  const [intervalLoading, setIntervalLoading] = useState(false);
  const [intervalReady, setIntervalReady] = useState(false);
  const [chartViewMode, setChartViewMode] = useState<ChartViewMode>("monitor");
  const [mainView, setMainView] = useState<MainView>("monitor");
  const [aiDrawerOpen, setAiDrawerOpen] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [reportPreview, setReportPreview] = useState(false);
  const [reportOptions, setReportOptions] = useState({
    charts: true,
    capability: true,
    abnormal: true,
    ai: false,
  });

  // 設定
  const [minSamples, setMinSamplesState] = useState<number>(5);
  const [minSamplesDraft, setMinSamplesDraft] = useState<number>(5);
  const [configLimits, setConfigLimits] = useState({
    min: 3,
    max: 100,
    default: 5,
  });
  const [cpkThreshold, setCpkThreshold] = useState<number>(1.33);
  const [cpkThresholdDraft, setCpkThresholdDraft] = useState<number>(1.33);
  const [cpkLimits, setCpkLimits] = useState({
    min: 0.5,
    max: 3,
    default: 1.33,
  });
  const [autoCreateControlLimit, setAutoCreateControlLimit] = useState(false);
  const [autoCreateDraft, setAutoCreateDraft] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);
  const [configMsg, setConfigMsg] = useState<{
    kind: "ok" | "err";
    text: string;
  } | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsPassword, setSettingsPassword] = useState("");

  // 管制圖
  const [data, setData] = useState<ChartApiResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  // 製程能力
  const [cap, setCap] = useState<CapabilityResponse | null>(null);
  const [capLoading, setCapLoading] = useState(false);
  const [capError, setCapError] = useState<string | null>(null);
  const [capRevision, setCapRevision] = useState(0);
  const [historyRows, setHistoryRows] = useState<ControlLimitVersion[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);

  // AI 摘要
  const [aiSummary, setAiSummary] = useState<string | null>(null);
  const [aiSummaryLoading, setAiSummaryLoading] = useState(false);
  const [aiSummaryError, setAiSummaryError] = useState<string | null>(null);

  // Phase I
  const [trialData, setTrialData] = useState<PhaseITrialResponse | null>(null);
  const [trialLoading, setTrialLoading] = useState(false);
  const [trialError, setTrialError] = useState<string | null>(null);
  const [excludedPointIds, setExcludedPointIds] = useState<Set<string>>(
    () => new Set(),
  );
  // 覆核草稿:使用者在畫面上選的原因與備註,按下儲存才送到後端。
  const [reviewDrafts, setReviewDrafts] = useState<
    Record<string, { reason: ExclusionReasonCode | ""; note: string }>
  >({});
  const [reviewSaving, setReviewSaving] = useState(false);
  const [reviewMessage, setReviewMessage] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);
  const [approvalMessage, setApprovalMessage] = useState<string | null>(null);
  const [chartRevision, setChartRevision] = useState(0);
  const chartRequestIdRef = useRef(0);
  const [autoBatch, setAutoBatch] = useState<{
    running: boolean;
    processed: number;
    total: number;
    results: AutoTrialItem[];
  }>({ running: false, processed: 0, total: 0, results: [] });
  const autoBatchRunningRef = useRef(false);
  const [autoBatchTrigger, setAutoBatchTrigger] = useState(0);
  const handledAutoBatchTriggerRef = useRef(0);
  const autoBatchReasonRef = useRef<"initial" | "enabled" | "manual" | null>(
    null,
  );
  const bootstrapStartedRef = useRef(false);

  const cancelPendingAutoBatch = useCallback(() => {
    autoBatchReasonRef.current = null;
    handledAutoBatchTriggerRef.current = autoBatchTrigger;
  }, [autoBatchTrigger]);

  // ────────────────────────────────────────────────────────
  // 首次載入 → 拿組合清單 + config。不可在載入時自動建立管制線。
  // ────────────────────────────────────────────────────────
  useEffect(() => {
    // React Strict Mode 在 dev 會重跑 mount effect；bootstrap 與初始批次只能排一次。
    if (bootstrapStartedRef.current) return;
    bootstrapStartedRef.current = true;
    const bootstrap = async () => {
      try {
        const r = await fetch("/api/processes", { cache: "no-store" });
        const j: { combos: FeatureCombo[] } = await r.json();
        const list = j.combos ?? [];
        setCombos(list);
        if (list.length > 0) {
          const first = list[0];
          setSelProduct(first.product);
          setSelProcess(first.process);
          setSelMachine(first.machine);
          setSelFeature(first.feature_name);
        }
      } catch {
        setError({ error: "無法載入組合清單" });
      }

      try {
        const r = await fetch("/api/config", { cache: "no-store" });
        const j = await r.json();
        if (typeof j.min_samples === "number") {
          setMinSamplesState(j.min_samples);
          setMinSamplesDraft(j.min_samples);
        }
        if (j.limits) setConfigLimits(j.limits);
        if (typeof j.cpk_threshold === "number") {
          setCpkThreshold(j.cpk_threshold);
          setCpkThresholdDraft(j.cpk_threshold);
        }
        if (j.cpk_limits) setCpkLimits(j.cpk_limits);
        if (typeof j.auto_create_control_limit === "boolean") {
          setAutoCreateControlLimit(j.auto_create_control_limit);
          setAutoCreateDraft(j.auto_create_control_limit);
          if (j.auto_create_control_limit) {
            // 頁面首次載入且設定已開啟：只排入一次批次。
            autoBatchReasonRef.current = "initial";
            setAutoBatchTrigger((value) => value + 1);
          }
        }
      } catch {
        /* 用預設 */
      }
    };
    void bootstrap();
  }, []);

  const saveConfig = async () => {
    if (!settingsPassword.trim()) {
      setConfigMsg({ kind: "err", text: "請輸入密碼" });
      return;
    }
    setSavingConfig(true);
    setConfigMsg(null);
    try {
      const r = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          min_samples: minSamplesDraft,
          cpk_threshold: cpkThresholdDraft,
          auto_create_control_limit: autoCreateDraft,
          password: settingsPassword,
        }),
      });
      const j = await r.json();
      if (!r.ok) {
        setConfigMsg({
          kind: "err",
          text: r.status === 401 ? "密碼錯誤" : (j.error ?? "設定失敗"),
        });
        return;
      }
      const wasAutoEnabled = autoCreateControlLimit;
      const nextAutoEnabled = Boolean(j.auto_create_control_limit);
      setMinSamplesState(j.min_samples);
      setCpkThreshold(j.cpk_threshold);
      setAutoCreateControlLimit(nextAutoEnabled);
      if (nextAutoEnabled && !wasAutoEnabled) {
        // 只有真的從關閉切成開啟才觸發；修改其他設定不重跑。
        autoBatchReasonRef.current = "enabled";
        setAutoBatchTrigger((value) => value + 1);
      }
      setConfigMsg({
        kind: "ok",
        text:
          `樣本數 ${j.min_samples} 筆、Cpk 門檻 ${j.cpk_threshold}、` +
          `自動建立${j.auto_create_control_limit ? "開啟" : "關閉"}`,
      });
      setSettingsPassword("");
      setCapRevision((v) => v + 1);
    } catch (e) {
      setConfigMsg({ kind: "err", text: String(e) });
    } finally {
      setSavingConfig(false);
    }
  };

  // ────────────────────────────────────────────────────────
  // 級聯選項
  // ────────────────────────────────────────────────────────
  const productOptions = useMemo(
    () => uniqueBy(combos, (c) => c.product).map((c) => c.product),
    [combos],
  );
  const processOptions = useMemo(
    () =>
      uniqueBy(
        combos.filter((c) => c.product === selProduct),
        (c) => c.process,
      ).map((c) => c.process),
    [combos, selProduct],
  );
  const machineOptions = useMemo(
    () =>
      uniqueBy(
        combos.filter(
          (c) => c.product === selProduct && c.process === selProcess,
        ),
        (c) => c.machine,
      ).map((c) => c.machine),
    [combos, selProduct, selProcess],
  );
  const featureOptions = useMemo(
    () =>
      uniqueBy(
        combos.filter(
          (c) =>
            c.product === selProduct &&
            c.process === selProcess &&
            c.machine === selMachine,
        ),
        (c) => c.feature_name,
      ),
    [combos, selProduct, selProcess, selMachine],
  );

  useEffect(() => {
    if (processOptions.length > 0 && !processOptions.includes(selProcess)) {
      setSelProcess(processOptions[0]);
    }
  }, [processOptions, selProcess]);

  useEffect(() => {
    if (machineOptions.length > 0 && !machineOptions.includes(selMachine)) {
      setSelMachine(machineOptions[0]);
    }
  }, [machineOptions, selMachine]);

  useEffect(() => {
    if (
      featureOptions.length > 0 &&
      !featureOptions.some((f) => f.feature_name === selFeature)
    ) {
      setSelFeature(featureOptions[0].feature_name);
    }
  }, [featureOptions, selFeature]);

  useEffect(() => {
    setTrialData(null);
    setTrialError(null);
    setExcludedPointIds(new Set());
    setApprovalMessage(null);
  }, [
    selProduct,
    selProcess,
    selMachine,
    selFeature,
    selChartType,
    selInterval,
    selEventType,
  ]);

  // ────────────────────────────────────────────────────────
  // 事件類型清單:換機台時重抓
  //   事件類型是 DB 的自由文字,不能寫死選項。
  //   目前選的類型若在新機台不存在,就退回 default_event_type,
  //   否則會拿一個查不到區間的類型去查,畫面會變成空的。
  // ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!selMachine) return;
    let cancelled = false;

    fetch(`/api/event-types?machine=${encodeURIComponent(selMachine)}`, {
      cache: "no-store",
    })
      .then(async (r) => {
        const j = await r.json();
        if (cancelled || !r.ok) return;
        const list: string[] = j.event_types ?? [];
        setEventTypes(list);
        setSelEventType((current) => {
          if (list.includes(current)) return current;
          return j.default_event_type ?? list[0] ?? DEFAULT_EVENT_TYPE;
        });
      })
      .catch(() => {
        if (!cancelled) setEventTypes([]);
      });

    return () => {
      cancelled = true;
    };
  }, [selMachine]);

  // ────────────────────────────────────────────────────────
  // 事件區間:機台/尺寸/事件類型變動時重抓,預設選最新一段
  // ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!selMachine) {
      setEventIntervals([]);
      setSelInterval(null);
      setIntervalReady(false);
      return;
    }
    let cancelled = false;
    setIntervalReady(false);
    setIntervalLoading(true);

    const url =
      `/api/event-intervals?machine=${encodeURIComponent(selMachine)}` +
      `&event_type=${encodeURIComponent(selEventType)}` +
      (selProduct ? `&product=${encodeURIComponent(selProduct)}` : "") +
      (selProcess ? `&process=${encodeURIComponent(selProcess)}` : "") +
      (selFeature ? `&feature=${encodeURIComponent(selFeature)}` : "");

    fetch(url, { cache: "no-store" })
      .then(async (r) => {
        const j = await r.json();
        if (cancelled) return;
        const list: EventIntervalOption[] = r.ok ? (j.intervals ?? []) : [];
        setEventIntervals(list);
        setSelInterval((current) => {
          if (list.length === 0) return null;
          // 事件區間是整張尺寸表共同的篩選條件。切換球標尺寸時即使該尺寸
          // 在目前區間沒有樣本，也不能偷偷換區間；否則 auto batch key 改變，
          // 只點一列就會被誤判成新範圍並重新跑整批試算。
          const kept = list.find((x) => x.interval_id === current);
          if (kept) return current;
          // 否則挑「最新且該尺寸真的有量測值」的一把。
          // 不能無腦選 list[0]:最新那把刀可能還沒量到這個尺寸,
          // 選下去會撈到零筆,Python 會回 422 measurements are required。
          const withData = list.find((x) => x.clean_sample_count > 0);
          return (withData ?? list[0]).interval_id;
        });
      })
      .catch(() => {
        if (!cancelled) {
          setEventIntervals([]);
          setSelInterval(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIntervalLoading(false);
          setIntervalReady(true);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [
    selMachine,
    selEventType,
    selProduct,
    selProcess,
    selFeature,
    chartRevision,
  ]);

  // ────────────────────────────────────────────────────────
  // 抓管制圖
  // ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!selProduct || !selProcess || !selMachine || !selFeature) return;
    if (!featureOptions.some((f) => f.feature_name === selFeature)) return;
    // 防競態:區間清單還在載入時先不要抓圖,否則會用上一台機台的
    // selInterval 去查,撈到零筆。等 selInterval 定案再抓。
    if (intervalLoading || !intervalReady) return;

    const requestId = ++chartRequestIdRef.current;
    const controller = new AbortController();
    setLoading(true);
    // 選擇已改變時不可繼續顯示上一組 key 的線；等這次 request 回來再畫。
    setData(null);
    setError(null);
    setAiSummary(null);
    setAiSummaryError(null);
    const url =
      `/api/chart?product=${encodeURIComponent(selProduct)}` +
      `&process=${encodeURIComponent(selProcess)}` +
      `&machine=${encodeURIComponent(selMachine)}` +
      `&feature=${encodeURIComponent(selFeature)}` +
      `&chart_type=${encodeURIComponent(selChartType)}` +
      `&event_type=${encodeURIComponent(selEventType)}` +
      (selInterval != null ? `&event_interval_id=${selInterval}` : "");
    fetch(url, { cache: "no-store", signal: controller.signal })
      .then(async (r) => {
        const j = await r.json();
        // 切尺寸／事件區間／核准刷新時會重疊發 request。只有最新 request
        // 可以寫入 state，避免較慢的舊 Phase I response 覆蓋 active response。
        if (controller.signal.aborted || requestId !== chartRequestIdRef.current) {
          return;
        }
        if (!r.ok) {
          setData(null);
          setError(j as ApiError);
        } else {
          setData(j as ChartApiResponse);
          if (j.has_active_control_limit === true) {
            setTrialData(null);
            setExcludedPointIds(new Set());
            setTrialError(null);
            // DB 已確認 active 後，移除這個尺寸先前的待處理色標。
            setAutoBatch((current) => ({
              ...current,
              results: current.results.filter(
                (item) => item.feature_name !== selFeature,
              ),
            }));
          }
        }
      })
      .catch((e) => {
        if (controller.signal.aborted || requestId !== chartRequestIdRef.current) {
          return;
        }
        setData(null);
        setError({ error: "請求失敗", detail: String(e) });
      })
      .finally(() => {
        if (!controller.signal.aborted && requestId === chartRequestIdRef.current) {
          setLoading(false);
        }
      });

    return () => controller.abort();
  }, [
    selProduct,
    selProcess,
    selMachine,
    selFeature,
    selChartType,
    selInterval,
    intervalLoading,
    intervalReady,
    featureOptions,
    chartRevision,
  ]);

  // ────────────────────────────────────────────────────────
  // 抓製程能力(CPK 表 + 指標 + 每日折線)
  // ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!selProduct || !selProcess || !selMachine) return;

    setCapLoading(true);
    setCapError(null);
    const url =
      `/api/capability?product=${encodeURIComponent(selProduct)}` +
      `&process=${encodeURIComponent(selProcess)}` +
      `&machine=${encodeURIComponent(selMachine)}` +
      (selFeature ? `&feature=${encodeURIComponent(selFeature)}` : "") +
      `&event_type=${encodeURIComponent(selEventType)}` +
      (selInterval != null ? `&event_interval_id=${selInterval}` : "");
    fetch(url, { cache: "no-store" })
      .then(async (r) => {
        const j = await r.json();
        if (!r.ok) {
          setCap(null);
          setCapError(j.detail ?? j.error ?? "製程能力計算失敗");
        } else {
          setCap(j as CapabilityResponse);
          if (typeof j.cpk_threshold === "number") {
            setCpkThreshold(j.cpk_threshold);
          }
        }
      })
      .catch((e) => {
        setCap(null);
        setCapError(String(e));
      })
      .finally(() => setCapLoading(false));
  }, [
    selProduct,
    selProcess,
    selMachine,
    selFeature,
    selInterval,
    capRevision,
  ]);

  // 歷史版本只讀取既有「管制圖」紀錄；沒有保存的歷史量測 window 不推測重建。
  useEffect(() => {
    if (!selProduct || !selProcess || !selMachine || !selFeature) return;
    const controller = new AbortController();
    setHistoryLoading(true);
    setHistoryError(null);
    const url =
      `/api/control-limit?product=${encodeURIComponent(selProduct)}` +
      `&process=${encodeURIComponent(selProcess)}` +
      `&machine=${encodeURIComponent(selMachine)}` +
      `&feature=${encodeURIComponent(selFeature)}`;
    fetch(url, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        const payload = await response.json();
        if (controller.signal.aborted) return;
        if (!response.ok) {
          setHistoryRows([]);
          setHistoryError(payload.detail ?? payload.error ?? "歷史版本載入失敗");
          return;
        }
        setHistoryRows((payload.rows ?? []) as ControlLimitVersion[]);
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setHistoryRows([]);
          setHistoryError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setHistoryLoading(false);
      });
    return () => controller.abort();
  }, [selProduct, selProcess, selMachine, selFeature, chartRevision]);

  // ────────────────────────────────────────────────────────
  // Phase I 試算 / 核准
  // ────────────────────────────────────────────────────────
  const phaseIRequestBody = useCallback(
    (excluded: Set<string>) => ({
      product: selProduct,
      process: selProcess,
      machine: selMachine,
      feature_name: selFeature,
      chart_type: selChartType,
      excluded_point_ids: Array.from(excluded),
      event_interval_id: selInterval,
      event_type: selEventType,
    }),
    [
      selProduct,
      selProcess,
      selMachine,
      selFeature,
      selChartType,
      selInterval,
      selEventType,
    ],
  );

  /** 統一刷新尺寸清單、active 狀態、chart、能力值與異常統計。 */
  const refreshDashboardData = useCallback(async () => {
    const combosResponse = await fetch("/api/processes", { cache: "no-store" });
    const combosPayload: { combos?: FeatureCombo[] } =
      await combosResponse.json();
    if (combosResponse.ok) setCombos(combosPayload.combos ?? []);
    setChartRevision((value) => value + 1);
    setCapRevision((value) => value + 1);
  }, []);

  /** 核准(或自動核准)成功後,清除 trial 並刷新所有衍生資料。 */
  const refreshAfterApproval = async (message: string) => {
    setApprovalMessage(message);
    setTrialData(null);
    setExcludedPointIds(new Set());
    setAutoBatch((current) => ({
      ...current,
      results: current.results.filter(
        (item) => item.feature_name !== selFeature,
      ),
    }));
    await refreshDashboardData();
  };

  /**
   * 依目前品號／製程／機台／圖型／事件類型／事件區間，逐一處理所有尺寸。
   * 每個尺寸仍走同一支 trial endpoint；後端再以 DB active 完整 key 擋重複，
   * 且只有設定開啟、無排除、上下圖皆無疑似異常時才呼叫共用 approve flow。
   */
  const runAutoTrialBatch = useCallback(async () => {
    if (
      autoBatchRunningRef.current ||
      !autoCreateControlLimit ||
      !selProduct ||
      !selProcess ||
      !selMachine ||
      !intervalReady
    ) {
      return;
    }

    const featureNames = featureOptions.map((item) => item.feature_name);
    if (featureNames.length === 0) return;

    autoBatchRunningRef.current = true;
    setAutoBatch({
      running: true,
      processed: 0,
      total: featureNames.length,
      results: [],
    });

    const results: AutoTrialItem[] = [];
    try {
      for (const featureName of featureNames) {
        let item: AutoTrialItem;
        try {
          const response = await fetch("/api/control-limit/trial", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              product: selProduct,
              process: selProcess,
              machine: selMachine,
              feature_name: featureName,
              chart_type: selChartType,
              excluded_point_ids: [],
              event_interval_id: selInterval,
              event_type: selEventType,
              auto_approve_if_clean: true,
            }),
          });
          const payload = await response.json();

          if (response.status === 409) {
            item = {
              feature_name: featureName,
              status: "already_active",
              reason: "已有 active 管制界線，已跳過。",
            };
          } else if (!response.ok) {
            item = {
              feature_name: featureName,
              status: response.status === 422 ? "skipped" : "failed",
              reason: payload.detail ?? payload.error ?? "自動試算失敗。",
            };
          } else if (payload.auto_approved === true) {
            item = {
              feature_name: featureName,
              status: "activated",
              reason: "試算通過，已建立 active 管制界線。",
            };
          } else {
            const count = Array.isArray(payload.suspected_points)
              ? payload.suspected_points.length
              : 0;
            item = {
              feature_name: featureName,
              status: "needs_review",
              reason:
                count > 0
                  ? `偵測到 ${count} 個疑似異常點，保留待人工確認。`
                  : "未自動啟用，請人工確認試算結果。",
            };
            if (featureName === selFeature) {
              setTrialData(payload as PhaseITrialResponse);
              setExcludedPointIds(new Set());
            }
          }
        } catch (err) {
          item = {
            feature_name: featureName,
            status: "failed",
            reason: err instanceof Error ? err.message : String(err),
          };
        }

        results.push(item);
        setAutoBatch({
          running: true,
          processed: results.length,
          total: featureNames.length,
          results: [...results],
        });
      }
    } finally {
      await refreshDashboardData().catch(() => undefined);
      autoBatchRunningRef.current = false;
      setAutoBatch({
        running: false,
        processed: results.length,
        total: featureNames.length,
        results,
      });
    }
  }, [
    autoCreateControlLimit,
    featureOptions,
    intervalReady,
    refreshDashboardData,
    selChartType,
    selEventType,
    selFeature,
    selInterval,
    selMachine,
    selProcess,
    selProduct,
  ]);

  // 批次只接受明確 trigger：首次載入、從關閉切成開啟、手動重新檢查。
  // 品號／製程／機台／圖型／尺寸切換只載入資料，絕不能隱式觸發試算。
  useEffect(() => {
    if (
      !autoCreateControlLimit ||
      autoBatchReasonRef.current == null ||
      autoBatchTrigger === 0 ||
      handledAutoBatchTriggerRef.current === autoBatchTrigger ||
      autoBatch.running ||
      !intervalReady ||
      featureOptions.length === 0
    ) {
      return;
    }
    // 先消耗 reason；後續任何 selection/render/effect 都不能重用這次 trigger。
    autoBatchReasonRef.current = null;
    handledAutoBatchTriggerRef.current = autoBatchTrigger;
    void runAutoTrialBatch();
  }, [
    autoCreateControlLimit,
    autoBatchTrigger,
    autoBatch.running,
    featureOptions.length,
    intervalReady,
    runAutoTrialBatch,
  ]);

  // 瀏覽到另一組條件時清除上一組的問題色標，但不觸發新批次。
  useEffect(() => {
    setAutoBatch((current) =>
      current.running
        ? current
        : { running: false, processed: 0, total: 0, results: [] },
    );
  }, [
    selProduct,
    selProcess,
    selMachine,
    selChartType,
    selInterval,
    selEventType,
  ]);

  /**
   * Phase I 試算。
   * allowAutoApprove 只在「首次試算」時傳 true;依排除項目重算一律傳 false,
   * 真正的自動核准條件由後端把關(設定開啟 + 無排除 + 無疑似異常點)。
   */
  const runPhaseITrial = async (
    excluded = excludedPointIds,
    allowAutoApprove = false,
  ) => {
    setTrialLoading(true);
    setTrialError(null);
    setApprovalMessage(null);
    try {
      const response = await fetch("/api/control-limit/trial", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...phaseIRequestBody(excluded),
          auto_approve_if_clean: allowAutoApprove,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        setTrialData(null);
        setTrialError(payload.detail ?? payload.error ?? "Phase I 試算失敗");
        return;
      }
      const trialPayload = payload as PhaseITrialResponse;
      if (trialPayload.auto_approved) {
        await refreshAfterApproval(
          trialPayload.message ?? "試算無異常,已自動建立 active 管制線。",
        );
        return;
      }
      setTrialData(trialPayload);
      setChartViewMode("analysis");
    } catch (err) {
      setTrialData(null);
      setTrialError(err instanceof Error ? err.message : String(err));
    } finally {
      setTrialLoading(false);
    }
  };

  const toggleExcludedPoint = (pointId: number | string) => {
    const key = String(pointId);
    setExcludedPointIds((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  /**
   * 疑似異常點清單。
   *
   * 優先用後端回來的 reviews —— 每個 point_id 一列(上下圖觸發同一點時已合併),
   * 而且帶著先前存過的覆核結果。
   *
   * 覆核表不可用時(例如某環境還沒跑過 db/migrations)退回 suspected_points,
   * 自行依 point_id 合併。功能降級成「只能勾選排除、不留紀錄」,但不會壞掉。
   */
  const suspectedRows = useMemo<SuspectedRow[]>(() => {
    if (!trialData) return [];

    if (trialData.reviews?.length) {
      return trialData.reviews
        .filter((review) => review.trial_violation)
        .map((review) => ({
          pointId: review.point_id,
          actualValue: review.actual_value,
          violatedRules: review.violated_rules,
          sources: review.sources,
          review,
        }));
    }

    const merged = new Map<string, SuspectedRow>();
    for (const point of trialData.suspected_points) {
      const id = String(point.point_id);
      const existing = merged.get(id);
      if (existing) {
        existing.violatedRules = Array.from(
          new Set([...existing.violatedRules, ...point.violated_rules]),
        );
        existing.sources.push({
          chart: point.chart,
          component_type: point.component_type,
        });
      } else {
        merged.set(id, {
          pointId: id,
          actualValue: point.actual_value,
          violatedRules: [...point.violated_rules],
          sources: [
            { chart: point.chart, component_type: point.component_type },
          ],
          review: null,
        });
      }
    }
    return Array.from(merged.values());
  }, [trialData]);

  const reviewTableUnavailable = Boolean(trialData?.review_error);

  /**
   * 試算結果一回來,就把先前存過的覆核結果帶回畫面:原因/備註填回草稿,
   * 上次判定為排除的點自動勾選。這正是「留紀錄」的價值 —— 換人接手、
   * 隔天再打開,都看得到當初排除了哪些點、理由是什麼。
   */
  useEffect(() => {
    if (!trialData) {
      setReviewDrafts({});
      setReviewMessage(null);
      return;
    }
    const drafts: Record<
      string,
      { reason: ExclusionReasonCode | ""; note: string }
    > = {};
    const previouslyExcluded = new Set<string>();
    for (const review of trialData.reviews ?? []) {
      drafts[review.point_id] = {
        reason: review.exclusion_reason_code ?? "",
        note: review.exclusion_note ?? "",
      };
      if (review.baseline_disposition === "exclude") {
        previouslyExcluded.add(review.point_id);
      }
    }
    setReviewDrafts(drafts);
    setReviewMessage(null);
    if (previouslyExcluded.size > 0) {
      setExcludedPointIds((current) => {
        const next = new Set(current);
        previouslyExcluded.forEach((id) => next.add(id));
        return next;
      });
    }
  }, [trialData]);

  const setReviewDraft = (
    pointId: string,
    patch: Partial<{ reason: ExclusionReasonCode | ""; note: string }>,
  ) => {
    setReviewDrafts((current) => ({
      ...current,
      [pointId]: {
        reason: current[pointId]?.reason ?? "",
        note: current[pointId]?.note ?? "",
        ...patch,
      },
    }));
  };

  /**
   * 把畫面上每個疑似異常點的處置寫進覆核表。
   *
   * 勾選 = exclude(必須有原因),未勾選 = retain。驗證規則跟後端與資料庫的
   * CHECK 約束一致,在這裡先擋只是為了給即時回饋 —— 真正把關的是 DB。
   */
  const saveTrialReviews = async () => {
    if (!trialData || suspectedRows.length === 0) return;
    setReviewSaving(true);
    setReviewMessage(null);
    setTrialError(null);
    try {
      for (const row of suspectedRows) {
        const draft = reviewDrafts[row.pointId] ?? { reason: "", note: "" };
        const excluded = excludedPointIds.has(row.pointId);

        if (excluded && !draft.reason) {
          setTrialError(`#${row.pointId} 排除時必須選擇原因。`);
          return;
        }
        if (excluded && draft.reason === "other" && !draft.note.trim()) {
          setTrialError(`#${row.pointId} 原因選「其他」時必須填補充說明。`);
          return;
        }

        const response = await fetch("/api/control-limit/review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...phaseIRequestBody(excludedPointIds),
            point_id: row.pointId,
            baseline_disposition: excluded ? "exclude" : "retain",
            exclusion_reason_code: excluded ? draft.reason : null,
            exclusion_note: excluded ? draft.note.trim() || null : null,
          }),
        });
        const payload = await response.json();
        if (!response.ok) {
          setTrialError(
            payload.error ?? `#${row.pointId} 覆核紀錄儲存失敗。`,
          );
          return;
        }
      }
      setReviewMessage(`已儲存 ${suspectedRows.length} 筆覆核紀錄。`);
    } catch (err) {
      setTrialError(err instanceof Error ? err.message : String(err));
    } finally {
      setReviewSaving(false);
    }
  };

  const approvePhaseITrial = async () => {
    if (!trialData) return;
    setApproving(true);
    setTrialError(null);
    setApprovalMessage(null);
    try {
      const response = await fetch("/api/control-limit/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(phaseIRequestBody(excludedPointIds)),
      });
      const payload = await response.json();
      if (!response.ok) {
        setTrialError(payload.detail ?? payload.error ?? "核准失敗");
        return;
      }
      await refreshAfterApproval(payload.message ?? "管制線已核准並啟用。");
    } catch (err) {
      setTrialError(err instanceof Error ? err.message : String(err));
    } finally {
      setApproving(false);
    }
  };

  const generateAiSummary = async () => {
    const summaryData = trialData?.chart ?? data;
    if (!summaryData) return;

    setAiSummaryLoading(true);
    setAiSummaryError(null);
    try {
      const response = await fetch("/api/ai-summary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chartData: summaryData }),
      });
      const payload = await response.json();
      if (!response.ok) {
        setAiSummary(null);
        setAiSummaryError(payload.detail ?? payload.error ?? "AI 摘要產生失敗");
        return;
      }
      setAiSummary(String(payload.summary ?? ""));
    } catch (err) {
      setAiSummary(null);
      setAiSummaryError(err instanceof Error ? err.message : String(err));
    } finally {
      setAiSummaryLoading(false);
    }
  };

  // ────────────────────────────────────────────────────────
  // 衍生資料
  // ────────────────────────────────────────────────────────
  const visibleData = trialData?.chart ?? data;

  const stats = useMemo(() => {
    if (!visibleData) return { normal: 0, control: 0, spec: 0 };
    let normal = 0;
    let control = 0;
    let spec = 0;
    for (const p of visibleData.points) {
      const s = pointStatus(p);
      if (s === "out_of_spec") spec += 1;
      else if (s === "out_of_control") control += 1;
      else normal += 1;
    }
    return { normal, control, spec };
  }, [visibleData]);

  // 異常清單拆成上圖 / 下圖兩個族群。
  // 上圖(I / XBAR)可能超規或失控;下圖(MR / R / S)只用自己的管制界線判失控。
  const abnormalByChart = useMemo(() => {
    if (!visibleData) {
      return {
        primary: [] as AbnormalListItem[],
        secondary: [] as AbnormalListItem[],
      };
    }

    const primary = visibleData.points
      .map(
        (point): AbnormalListItem => ({
          x: point.x,
          value: point.value,
          status: pointStatus(point),
          violatedRules: point.violated_rules,
        }),
      )
      .filter((item) => item.status !== "normal");

    const secondary = visibleData.secondary_chart
      ? visibleData.secondary_chart.points
          .map(
            (point): AbnormalListItem => ({
              x: point.x,
              value: point.value,
              status: pointStatus(point),
              violatedRules: point.violated_rules,
            }),
          )
          .filter((item) => item.status !== "normal")
      : (visibleData.mr?.points ?? [])
          .filter((point) => point.value != null && point.is_out_of_control)
          .map(
            (point): AbnormalListItem => ({
              x: point.x,
              value: point.value as number,
              status: "out_of_control",
              violatedRules: point.violated_rules ?? [],
            }),
          );

    return { primary, secondary };
  }, [visibleData]);

  const abnormalCount =
    abnormalByChart.primary.length + abnormalByChart.secondary.length;

  const autoProblemByFeature = useMemo(() => {
    const result = new Map<string, AutoTrialItem>();
    for (const item of autoBatch.results) {
      if (
        item.status === "needs_review" ||
        item.status === "skipped" ||
        item.status === "failed"
      ) {
        result.set(item.feature_name, item);
      }
    }
    return result;
  }, [autoBatch.results]);

  const selectedComboForStatus = featureOptions.find(
    (item) => item.feature_name === selFeature,
  );
  const activeHistoryVersion = historyRows.find(
    (row) =>
      row.product.trim() === selProduct.trim() &&
      row.process.trim() === selProcess.trim() &&
      row.machine.trim() === selMachine.trim() &&
      row.feature_name.trim() === selFeature.trim() &&
      row.chart_type === selChartType &&
      row.is_active,
  );
  const comboHasActiveForChart = Boolean(
    selectedComboForStatus?.active_chart_types?.includes(selChartType) ??
      (selectedComboForStatus?.has_active_control_limit &&
        selectedComboForStatus.chart_type === selChartType),
  );
  // chart、版本 API、尺寸清單都來自 DB；任一回應確認完整 key active 就維持 Phase II。
  // 特別是目前事件區間沒有量測點而 chart 回 404 時，不可錯退回 Phase I。
  const hasActiveLimit = Boolean(
    data?.has_active_control_limit ||
      data?.limits.cl != null ||
      activeHistoryVersion ||
      comboHasActiveForChart,
  );
  const activeInterval =
    eventIntervals.find((it) => it.interval_id === selInterval) ?? null;
  const metrics = cap?.metrics ?? null;
  const isGood = metrics?.cpk != null && metrics.cpk >= cpkThreshold;
  const titles = chartTitles(selChartType);
  // Recharts 在資料點數不變、只有 ReferenceLine 值改變時偶爾會保留舊圖層。
  // 將正式／trial 界線納入 key，界線版本改變就重建 chart instance。
  const primaryChartRenderKey = [
    selProduct,
    selProcess,
    selMachine,
    selFeature,
    selChartType,
    selInterval ?? "all",
    selEventType,
    trialData ? "trial" : "db",
    visibleData?.limits.ucl ?? "none",
    visibleData?.limits.cl ?? "none",
    visibleData?.limits.lcl ?? "none",
  ].join("|");
  const secondaryChartRenderKey = [
    primaryChartRenderKey,
    visibleData?.mr?.limits?.ucl ?? "none",
    visibleData?.mr?.limits?.cl ?? "none",
    visibleData?.mr?.limits?.lcl ?? "none",
  ].join("|");
  // 上圖 / 下圖的元件代號,用在異常清單的來源標籤
  const primaryComponent =
    visibleData?.primary_chart?.component_type ??
    (selChartType === "I-MR" ? "I" : "XBAR");
  const secondaryComponent =
    visibleData?.secondary_chart?.component_type ??
    (selChartType === "I-MR" ? "MR" : selChartType === "Xbar-R" ? "R" : "S");

  const latestPoint = visibleData?.points.at(-1) ?? null;
  const selectedCombo = selectedComboForStatus;
  const currentPhase = hasActiveLimit ? "Phase II" : "Phase I";
  const selectedTrialProblem = autoProblemByFeature.get(selFeature);
  const selectedStatus =
    stats.spec > 0 ? "超規" : stats.control > 0 ? "失控" : "正常";

  const dimensionStatus = (feature: FeatureCombo) => {
    const activeForCurrentChart =
      feature.active_chart_types?.includes(selChartType) ??
      (feature.has_active_control_limit && feature.chart_type === selChartType);
    if (activeForCurrentChart) return "Active";
    if (
      feature.feature_name === selFeature &&
      (trialData || selectedTrialProblem?.status === "needs_review")
    ) {
      return "Trial";
    }
    return "未啟用";
  };

  const toggleReportOption = (key: keyof typeof reportOptions) => {
    setReportOptions((current) => ({ ...current, [key]: !current[key] }));
  };

  const printReport = () => {
    setReportOpen(false);
    setReportPreview(true);
    window.setTimeout(() => window.print(), 80);
  };

  return (
    <div className={`dash view-${mainView} ${reportPreview ? "report-ready" : ""}`}>
      {/* ───────────── ① 灰 選擇 ───────────── */}
      <section className="box box-gray toolbar-box">
        <div className="brand-mark" aria-label="SPC 製程監控">
          <span className="brand-icon">◇</span>
          <b>SPC 製程監控</b>
        </div>

        <label className="mini-field">
          <span>品號</span>
          <select
            value={selProduct}
            onChange={(e) => {
              cancelPendingAutoBatch();
              setSelProduct(e.target.value);
            }}
            disabled={autoBatch.running}
          >
            {productOptions.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>

        <label className="mini-field">
          <span>製程</span>
          <select
            value={selProcess}
            onChange={(e) => {
              cancelPendingAutoBatch();
              setSelProcess(e.target.value);
            }}
            disabled={autoBatch.running}
          >
            {processOptions.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>

        <label className="mini-field">
          <span>機台</span>
          <select
            value={selMachine}
            onChange={(e) => {
              cancelPendingAutoBatch();
              setSelMachine(e.target.value);
            }}
            disabled={autoBatch.running}
          >
            {machineOptions.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>

        <label className="mini-field">
          <span>事件類型</span>
          <select
            value={selEventType}
            onChange={(e) => {
              // 換事件類型等同換一套區間切法：先清掉目前選的區間，
              // 讓區間 effect 重新決定預設值。若沿用舊 interval_id，
              // 那個 id 屬於別種事件，查出來會是零筆。
              cancelPendingAutoBatch();
              setSelInterval(null);
              setSelEventType(e.target.value);
            }}
            disabled={autoBatch.running || eventTypes.length === 0}
            title={
              eventTypes.length === 0
                ? "此機台沒有任何事件紀錄"
                : "事件類型決定區間怎麼切；換刀、保養各自切各自的區間"
            }
            style={{ maxWidth: 120 }}
          >
            {eventTypes.length === 0 && (
              <option value={selEventType}>(無事件紀錄)</option>
            )}
            {eventTypes.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>

        <label className="mini-field">
          <span>事件區間</span>
          <select
            value={selInterval == null ? "" : String(selInterval)}
            onChange={(e) => {
              cancelPendingAutoBatch();
              setSelInterval(e.target.value === "" ? null : Number(e.target.value));
            }}
            disabled={
              autoBatch.running || intervalLoading || eventIntervals.length === 0
            }
            title={
              eventIntervals.length === 0
                ? `此機台沒有「${selEventType}」的事件紀錄,顯示全部樣本`
                : `選擇一段「${selEventType}」區間;每段區間是一張獨立的管制圖`
            }
            style={{ maxWidth: 190 }}
          >
            {eventIntervals.length === 0 && (
              <option value="">
                {intervalLoading ? "載入中…" : `(無${selEventType}紀錄)`}
              </option>
            )}
            {eventIntervals.map((it) => (
              <option key={it.interval_id} value={String(it.interval_id)}>
                {shortStamp(it.changed_at)} · #{it.serial_from}~
                {it.serial_to_exclusive == null
                  ? "now"
                  : it.serial_to_exclusive - 1}
                {it.clean_sample_count === 0
                  ? " (無此尺寸資料)"
                  : it.control_start_time
                    ? " ✓"
                    : ` (${it.clean_sample_count}/${minSamples})`}
              </option>
            ))}
          </select>
        </label>

        <label className="mini-field">
          <span>管制圖類型</span>
          <select
            value={selChartType}
            onChange={(e) => {
              cancelPendingAutoBatch();
              setSelChartType(e.target.value as ChartType);
            }}
            disabled={autoBatch.running}
          >
            {CHART_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>

        <span className="toolbar-note">
          {selFeature || "未選尺寸"}
          {activeInterval &&
            ` · 管制起 ${
              activeInterval.control_start_time
                ? fmtTime(activeInterval.control_start_time)
                : `未達門檻 ${activeInterval.clean_sample_count}/${minSamples}`
            }`}
          {" · "}Cpk 門檻 {cpkThreshold} · 樣本 {minSamples}
        </span>

        <button
          className="btn ghost toolbar-action"
          onClick={() => setAiDrawerOpen(true)}
        >
          AI 摘要
        </button>
        <button
          className="btn ghost toolbar-action"
          onClick={() => setReportOpen(true)}
        >
          報表
        </button>

        <button
          className="gear"
          onClick={() => {
            setSettingsOpen(true);
            setConfigMsg(null);
            setMinSamplesDraft(minSamples);
            setCpkThresholdDraft(cpkThreshold);
            setAutoCreateDraft(autoCreateControlLimit);
          }}
          title="設定"
          aria-label="設定"
        >
          ⚙
        </button>
      </section>

      {error && (
        <div className="flash err">
          <b>{error.error}</b>
          {error.spc_api_base && (
            <span> — 請確認 Python SPC 服務已啟動({error.spc_api_base})</span>
          )}
          {error.detail && <span className="muted"> {error.detail}</span>}
        </div>
      )}
      {approvalMessage && <div className="flash ok">{approvalMessage}</div>}

      <section className="status-strip" aria-label="目前狀態摘要">
        <article className={`status-card phase ${hasActiveLimit ? "active" : "trial"}`}>
          <span className="status-label">目前狀態</span>
          <strong>{currentPhase}</strong>
          <small>{hasActiveLimit ? "正式監控中" : "建立基準中"}</small>
        </article>
        <article className={`status-card ${selectedStatus === "正常" ? "good" : "danger"}`}>
          <span className="status-label">異常狀態</span>
          <strong>{selectedStatus}</strong>
          <small>失控 {stats.control} · 超規 {stats.spec}</small>
        </article>
        <article className={`status-card ${hasActiveLimit ? "active" : "muted-card"}`}>
          <span className="status-label">管制界線</span>
          <strong>{hasActiveLimit ? "Active" : trialData ? "Trial" : "尚未啟用"}</strong>
          {hasActiveLimit ? (
            <small className="limit-summary">
              <span>UCL {fmt(data?.limits.ucl ?? activeHistoryVersion?.ucl)}</span>
              <span>CL {fmt(data?.limits.cl ?? activeHistoryVersion?.cl)}</span>
              <span>LCL {fmt(data?.limits.lcl ?? activeHistoryVersion?.lcl)}</span>
            </small>
          ) : (
            <small>Phase I 暫估／規格判定</small>
          )}
        </article>
        <article className="status-card latest">
          <span className="status-label">最新量測</span>
          <strong>{latestPoint ? fmt(latestPoint.value) : "—"}</strong>
          <small>{latestPoint?.time ? fmtTime(latestPoint.time) : "尚無資料"}</small>
        </article>
        <article className="status-card sample">
          <span className="status-label">樣本數</span>
          <strong>{visibleData?.points.length ?? selectedCombo?.sample_size ?? 0}</strong>
          <small>目前事件區間有效樣本</small>
        </article>
        <article className={`status-card capability ${isGood ? "good" : ""}`}>
          <span className="status-label">能力概況{hasActiveLimit ? "" : "（暫估）"}</span>
          <strong>Cpk {fmt(metrics?.cpk, 2)}</strong>
          <small>Cp {fmt(metrics?.cp, 2)} · Ppk {fmt(metrics?.ppk, 2)}</small>
        </article>
      </section>

      <div className="app-workspace">
        <aside className="dimension-panel" aria-label="球標尺寸清單">
          <div className="dimension-head">
            <div>
              <strong>尺寸清單</strong>
              <small>{featureOptions.length} 個球標尺寸</small>
            </div>
          </div>
          <div className="dimension-list">
            {featureOptions.map((feature, index) => {
              const status = dimensionStatus(feature);
              const problem = autoProblemByFeature.get(feature.feature_name);
              return (
                <button
                  key={feature.feature_name}
                  type="button"
                  className={`dimension-row ${feature.feature_name === selFeature ? "selected" : ""} ${problem ? `problem-${problem.status}` : ""}`}
                  onClick={() => setSelFeature(feature.feature_name)}
                  disabled={autoBatch.running}
                  title={problem?.reason}
                >
                  <span className="dimension-index">{index + 1}</span>
                  <span className="dimension-name">{feature.feature_name}</span>
                  <span className="dimension-value">{fmt(feature.latest_value)}</span>
                  <span className={`status-badge ${status.toLowerCase()}`}>{status}</span>
                </button>
              );
            })}
          </div>
        </aside>

        <main className="content-shell">
          <nav className="main-tabs" aria-label="主功能">
            <button className={mainView === "monitor" ? "active" : ""} onClick={() => setMainView("monitor")}>即時監控</button>
            <button className={mainView === "capability" ? "active" : ""} onClick={() => setMainView("capability")}>製程能力</button>
            <button className={mainView === "history" ? "active" : ""} onClick={() => setMainView("history")}>歷史版本</button>
            <span className="tab-context">{selFeature} · {selChartType}</span>
          </nav>

      <div className="dash-body">
        <div className="dash-main">
          {/* ───────────── ② 藍 製程能力 ───────────── */}
          <section className="box box-blue cap-box">
            <div className="box-head">
              <span className="box-tag">製程能力指標 {hasActiveLimit ? "" : "（Phase I 暫估）"}</span>
              {capLoading && <span className="tiny muted">計算中…</span>}
              {!capLoading && capError && (
                <span className="tiny err-text">{capError}</span>
              )}
              {!capLoading && !capError && cap?.persisted && (
                <span
                  className={`tiny ${cap.persisted.errors.length > 0 ? "err-text" : "muted"}`}
                  title={
                    cap.persisted.errors.length > 0
                      ? cap.persisted.errors.join("\n")
                      : "已把 cp/cpk/cpm/cpmk/ppk 寫回管制圖 active 版本"
                  }
                >
                  已回寫 {cap.persisted.updated} 筆
                  {cap.persisted.skipped > 0 &&
                    `,Phase I 略過 ${cap.persisted.skipped}`}
                  {cap.persisted.errors.length > 0 &&
                    `,失敗 ${cap.persisted.errors.length}`}
                </span>
              )}
            </div>

            <div className="cap-grid">
              {/* 左:CPK 統計表 */}
              <div className="card cap-table-card">
                <table className="cap-table">
                  <thead>
                    <tr>
                      <th className="w-idx">#</th>
                      <th>球標名</th>
                      <th className="w-num">標準</th>
                      <th className="w-tol">上</th>
                      <th className="w-tol">下</th>
                      <th className="w-cpk">CPK</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(cap?.table ?? []).map((row) => {
                      const active = row.feature_name === selFeature;
                      const autoProblem = autoProblemByFeature.get(
                        row.feature_name,
                      );
                      return (
                        <tr
                          key={row.feature_name}
                          className={[
                            active ? "row-active" : "",
                            autoProblem
                              ? `row-auto-${autoProblem.status.replace("_", "-")}`
                              : "",
                          ]
                            .filter(Boolean)
                            .join(" ")}
                          onClick={() => {
                            if (!autoBatch.running) {
                              cancelPendingAutoBatch();
                              setSelFeature(row.feature_name);
                            }
                          }}
                          title={
                            autoProblem
                              ? `${row.feature_name}：${autoProblem.reason}`
                              : (row.error ?? row.feature_name)
                          }
                        >
                          <td>{row.index}</td>
                          <td className="cell-name">
                            {autoProblem && (
                              <span
                                className={`auto-problem-dot ${autoProblem.status.replace("_", "-")}`}
                                aria-label={autoProblem.reason}
                              />
                            )}
                            {row.feature_name}
                          </td>
                          <td className="num">{row.nominal_value}</td>
                          <td className="num">
                            {fmtTol(row.upper_tolerance)}
                          </td>
                          <td className="num">
                            {fmtTol(row.lower_tolerance)}
                          </td>
                          <td
                            className="num"
                            style={{
                              color: active
                                ? "#fff"
                                : cpkColor(row.cpk, cpkThreshold),
                              fontWeight: 600,
                            }}
                          >
                            {row.cpk == null ? "—" : row.cpk.toFixed(2)}
                          </td>
                        </tr>
                      );
                    })}
                    {(cap?.table ?? []).length === 0 && !capLoading && (
                      <tr>
                        <td colSpan={6} className="muted center">
                          無資料
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {/* 右:兩條指標橫條 + 每日折線 */}
              <div className="cap-right">
                <div className="card metric-row">
                  <div className="metric">
                    <span className="m-label">CPK</span>
                    <span
                      className="m-value big"
                      style={{ color: cpkColor(metrics?.cpk ?? null, cpkThreshold) }}
                    >
                      {metrics?.cpk == null ? "—" : metrics.cpk.toFixed(2)}
                    </span>
                  </div>
                  <div
                    className={`verdict ${metrics?.cpk == null ? "none" : isGood ? "good" : "bad"}`}
                  >
                    <span className="v-text">
                      {metrics?.cpk == null ? "—" : isGood ? "良品" : "不良"}
                    </span>
                    <span className="v-sub">
                      {metrics?.cpk == null
                        ? "無資料"
                        : `${isGood ? "達" : "低於"} ${cpkThreshold}`}
                    </span>
                  </div>
                  <div className="metric">
                    <span className="m-label">Cp</span>
                    <span className="m-value">{fmt(metrics?.cp, 2)}</span>
                  </div>
                  <div className="metric">
                    <span className="m-label">Ppk</span>
                    <span className="m-value">{fmt(metrics?.ppk, 2)}</span>
                  </div>
                  <div className="metric">
                    <span className="m-label">Cpm</span>
                    <span className="m-value">{fmt(metrics?.cpm, 2)}</span>
                  </div>
                  <div className="metric">
                    <span className="m-label">Cpmk</span>
                    <span className="m-value">{fmt(metrics?.cpmk, 2)}</span>
                  </div>
                </div>

                <div className="card metric-row">
                  <div className="metric">
                    <span className="m-label">平均 X-bar</span>
                    <span className="m-value">{fmt(metrics?.mean)}</span>
                  </div>
                  <div className="metric">
                    <span className="m-label">標準差 σ</span>
                    <span className="m-value">{fmt(metrics?.sigma)}</span>
                  </div>
                  <div className="metric">
                    <span className="m-label">USL</span>
                    <span className="m-value red">{fmt(metrics?.usl)}</span>
                  </div>
                  <div className="metric">
                    <span className="m-label">LSL</span>
                    <span className="m-value red">{fmt(metrics?.lsl)}</span>
                  </div>
                  <div className="metric">
                    <span className="m-label">量測次數</span>
                    <span className="m-value">
                      {metrics?.sample_size ?? "—"}
                    </span>
                  </div>
                  <div className="metric">
                    <span className="m-label">量測天數</span>
                    <span className="m-value">{cap?.daily?.day_count ?? "—"}</span>
                  </div>
                </div>

                <div className="card trend-card">
                  <div className="card-head">
                    <span>生產穩定度 — {trendLabel(cap?.daily?.bucket)}</span>
                    <span className="tiny muted">
                      最後量測 {fmtTime(cap?.daily?.last_measured_at ?? null)}
                    </span>
                  </div>
                  <div className="trend-body">
                    <DailyTrendChart
                      points={cap?.daily?.points ?? []}
                      usl={metrics?.usl ?? null}
                      lsl={metrics?.lsl ?? null}
                      standard={metrics?.nominal_value ?? null}
                      height={118}
                    />
                  </div>
                </div>
              </div>
            </div>
          </section>

          {/* ───────────── ③ 綠 管制監控 ───────────── */}
          <section className="box box-green chart-box">
            <div className="box-head">
              <span className="box-tag">管制圖監控</span>
              <div className="mode-toggle" aria-label="管制圖檢視模式">
                <button
                  type="button"
                  className={chartViewMode === "monitor" ? "active" : ""}
                  onClick={() => setChartViewMode("monitor")}
                >
                  監管
                </button>
                <button
                  type="button"
                  className={chartViewMode === "analysis" ? "active" : ""}
                  onClick={() => setChartViewMode("analysis")}
                >
                  分析
                </button>
              </div>
            </div>

            {!hasActiveLimit && data && !trialData && (
              <div className="card notice-inline">
                尚未建立 <b>{selChartType}</b> 管制界線(Phase I),僅依 USL/LSL
                判定超規。
                {activeInterval &&
                  (activeInterval.control_start_time ? (
                    <>
                      {" "}
                      本區間已於 <b>{fmtTime(activeInterval.control_start_time)}</b>{" "}
                      累積滿 {minSamples} 筆,可在右側「⑤ 決策」試算並核准。
                    </>
                  ) : (
                    <>
                      {" "}
                      本區間乾淨樣本 {activeInterval.clean_sample_count}/
                      {minSamples},尚未達門檻。
                    </>
                  ))}
              </div>
            )}

            {loading && <p className="muted tiny">載入中…</p>}

            {!loading && visibleData && (
              <div className="chart-pair">
                <div className="card chart-card">
                  <div className="card-head">
                    <span>{titles.top}</span>
                  </div>
                  <div className="chart-body">
                    <ControlChart
                      key={primaryChartRenderKey}
                      data={visibleData}
                      mode={chartViewMode}
                      height={190}
                    />
                  </div>
                </div>

                <div className="card chart-card">
                  <div className="card-head">
                    <span>{titles.bottom}</span>
                  </div>
                  <div className="chart-body">
                    {visibleData.mr ? (
                      <MovingRangeChart
                        key={secondaryChartRenderKey}
                        data={visibleData.mr}
                        mode={chartViewMode}
                        height={190}
                      />
                    ) : (
                      <p className="muted tiny">
                        尚無次要圖資料(需已啟用管制界線)。
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )}
          </section>

          <section className="box history-box">
            <div className="box-head">
              <span className="box-tag">管制界線版本</span>
              {historyLoading && <span className="tiny muted">載入中…</span>}
            </div>
            {historyError && <div className="flash err">{historyError}</div>}
            {!historyLoading && historyRows.length === 0 && !historyError && (
              <div className="history-empty">此尺寸尚無管制界線版本。</div>
            )}
            {historyRows.length > 0 && (
              <div className="history-table-wrap">
                <table className="history-table">
                  <thead><tr><th>版本起始</th><th>結束</th><th>圖型</th><th>狀態</th><th>UCL</th><th>CL</th><th>LCL</th><th>樣本</th><th>Cpk</th></tr></thead>
                  <tbody>
                    {historyRows.map((row) => (
                      <tr key={`${row.chart_type}-${row.started_at}`}>
                        <td>{fmtTime(row.started_at)}</td>
                        <td>{fmtTime(row.ended_at)}</td>
                        <td>{row.chart_type}</td>
                        <td><span className={`status-badge ${row.is_active ? "active" : "closed"}`}>{row.is_active ? "Active" : "已結束"}</span></td>
                        <td>{fmt(row.ucl)}</td><td>{fmt(row.cl)}</td><td>{fmt(row.lcl)}</td>
                        <td>{row.sample_size ?? "—"}</td><td>{fmt(row.cpk, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="history-note">目前資料庫保存版本界線與能力值，未保存每個版本當時的完整量測 window，因此不推測繪製歷史曲線。</p>
          </section>
        </div>

        <div className="dash-side">
          {/* ───────────── ④ 橘 異常判定 ───────────── */}
          <section className="box box-amber judge-box">
            <div className="box-head">
              <span className="box-tag">異常判定</span>
            </div>

            <div className="stat-row">
              <div className="stat" style={{ background: STATUS_COLOR.normal }}>
                <div className="s-num">{stats.normal}</div>
                <div className="s-lbl">標準</div>
              </div>
              <div
                className="stat"
                style={{ background: STATUS_COLOR.out_of_control }}
              >
                <div className="s-num">{stats.control}</div>
                <div className="s-lbl">失控</div>
              </div>
              <div
                className="stat"
                style={{ background: STATUS_COLOR.out_of_spec }}
              >
                <div className="s-num">{stats.spec}</div>
                <div className="s-lbl">超規</div>
              </div>
            </div>
            <div className="stat-note">
              以上為<b>上圖</b>({primaryComponent})統計
            </div>

            <div className="card list-card">
              <div className="card-head">
                <span>異常點清單 ({abnormalCount})</span>
              </div>
              <div className="ab-sections">
                <AbnormalSection
                  title={`上圖 ${primaryComponent}`}
                  chartLabel={primaryComponent}
                  tone="primary"
                  items={abnormalByChart.primary}
                />
                <AbnormalSection
                  title={`下圖 ${secondaryComponent}`}
                  chartLabel={secondaryComponent}
                  tone="secondary"
                  items={abnormalByChart.secondary}
                />
              </div>
            </div>
          </section>

          {/* ───────────── ⑤ 紫 決策 ───────────── */}
          <section className="box box-purple decide-box">
            <div className="box-head">
              <span className="box-tag">決策 / 下一步</span>
              {hasActiveLimit && (
                <span className="tiny muted">Phase II 監控中</span>
              )}
            </div>

            {autoCreateControlLimit && (
              <div className="card auto-trial-card">
                <div className="card-head">
                  <span>
                    {autoBatch.running ? "自動試算中" : "自動試算批次"}
                  </span>
                  <span className="tiny muted">
                    已處理 {autoBatch.processed} / {autoBatch.total}
                  </span>
                </div>
                {autoBatch.running && (
                  <progress
                    max={Math.max(autoBatch.total, 1)}
                    value={autoBatch.processed}
                  />
                )}
                {!autoBatch.running && autoBatch.results.length > 0 && (
                  <div className="auto-trial-results tiny">
                    {autoBatch.results
                      .filter(
                        (item) =>
                          item.status !== "already_active" &&
                          item.status !== "activated",
                      )
                      .map((item) => (
                        <div key={item.feature_name}>
                          <b>{item.feature_name}</b>：{item.reason}
                        </div>
                      ))}
                    {autoBatch.results.every(
                      (item) =>
                        item.status === "already_active" ||
                        item.status === "activated",
                    ) && <span className="ok-text">所有尺寸皆已進入 Phase II。</span>}
                  </div>
                )}
                <button
                  className="btn ghost xs"
                  onClick={() => {
                    autoBatchReasonRef.current = "manual";
                    setAutoBatchTrigger((value) => value + 1);
                  }}
                  disabled={autoBatch.running || !intervalReady}
                >
                  {autoBatch.running ? "批次處理中…" : "重新批次檢查"}
                </button>
              </div>
            )}

            {!hasActiveLimit && (
              <>
                <div className="card trial-card">
                  <div className="card-head">
                    <span>
                      Phase I {trialData ? "疑似異常點 — 勾選排除" : "尚未試算"}
                    </span>
                    <span className="tiny muted">
                      自動建立{autoCreateControlLimit ? "開" : "關"}
                    </span>
                  </div>
                  <div className="trial-list">
                    {suspectedRows.length ? (
                      suspectedRows.map((row) => {
                        const excluded = excludedPointIds.has(row.pointId);
                        const draft = reviewDrafts[row.pointId] ?? {
                          reason: "" as ExclusionReasonCode | "",
                          note: "",
                        };
                        const reviewed =
                          row.review?.review_status === "reviewed";
                        return (
                          <div key={row.pointId} className="trial-item-block">
                            <label
                              className="trial-item"
                              title={row.violatedRules.map(ruleLabel).join("、")}
                            >
                              <input
                                type="checkbox"
                                checked={excluded}
                                onChange={() =>
                                  toggleExcludedPoint(row.pointId)
                                }
                              />
                              <span>
                                {row.sources.map((src) => (
                                  <span
                                    key={`${src.chart}-${src.component_type}`}
                                    className={`trial-src ${src.chart}`}
                                  >
                                    {src.chart === "primary" ? "上" : "下"}
                                    {src.component_type}
                                  </span>
                                ))}
                                #{row.pointId} · {row.actualValue}
                                {!reviewTableUnavailable && (
                                  <span
                                    className={`review-tag ${
                                      reviewed ? "done" : "pending"
                                    }`}
                                  >
                                    {reviewed ? "已覆核" : "未覆核"}
                                  </span>
                                )}
                              </span>
                            </label>
                            {excluded && !reviewTableUnavailable && (
                              <div className="review-fields">
                                <select
                                  value={draft.reason}
                                  onChange={(e) =>
                                    setReviewDraft(row.pointId, {
                                      reason: e.target.value as
                                        | ExclusionReasonCode
                                        | "",
                                    })
                                  }
                                >
                                  <option value="">選擇排除原因…</option>
                                  {EXCLUSION_REASON_OPTIONS.map((opt) => (
                                    <option key={opt.value} value={opt.value}>
                                      {opt.label}
                                    </option>
                                  ))}
                                </select>
                                {draft.reason === "other" && (
                                  <input
                                    type="text"
                                    placeholder="補充說明(必填)"
                                    value={draft.note}
                                    onChange={(e) =>
                                      setReviewDraft(row.pointId, {
                                        note: e.target.value,
                                      })
                                    }
                                  />
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })
                    ) : trialData ? (
                      <span className="ok-text tiny">
                        本次試算未偵測到疑似異常點,可直接核准。
                      </span>
                    ) : (
                      <span className="muted tiny">
                        按「試算」以計算 Phase I 界線。
                      </span>
                    )}
                  </div>
                  {trialData && (
                    <div className="trial-limits tiny">
                      CL {fmt(trialData.trial.cl)} · UCL{" "}
                      {fmt(trialData.trial.ucl)} · LCL{" "}
                      {fmt(trialData.trial.lcl)} · 樣本{" "}
                      {trialData.sample_count}
                      {trialData.subgroup_count != null
                        ? ` / ${trialData.subgroup_count} 組`
                        : ""}
                      {trialData.control_start_time && (
                        <div>
                          管制開始 {fmtTime(trialData.control_start_time)}
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {reviewTableUnavailable && (
                  <div className="flash warn tiny">
                    覆核紀錄不可用（資料庫尚未套用 db/migrations）。勾選排除仍然
                    有效，但不會留下稽核紀錄。
                  </div>
                )}
                {trialError && <div className="flash err tiny">{trialError}</div>}
                {reviewMessage && (
                  <div className="flash ok tiny">{reviewMessage}</div>
                )}

                <div className="btn-row">
                  <button
                    className="btn ghost"
                    onClick={() => void saveTrialReviews()}
                    disabled={
                      !trialData ||
                      suspectedRows.length === 0 ||
                      reviewSaving ||
                      trialLoading ||
                      reviewTableUnavailable
                    }
                    title="把每個疑似異常點的處置與原因寫進覆核紀錄"
                  >
                    {reviewSaving ? "儲存中…" : "儲存覆核"}
                  </button>
                  <button
                    className="btn ghost"
                    onClick={() =>
                      void (trialData
                        ? runPhaseITrial(excludedPointIds, false)
                        : runPhaseITrial(new Set<string>(), true))
                    }
                    disabled={trialLoading || autoBatch.running}
                  >
                    {trialLoading ? "試算中…" : trialData ? "依排除重算" : "試算"}
                  </button>
                  <button
                    className="btn primary"
                    onClick={() => void approvePhaseITrial()}
                    disabled={
                      !trialData || approving || trialLoading || autoBatch.running
                    }
                  >
                    {approving ? "核准中…" : "核准啟用"}
                  </button>
                </div>
              </>
            )}

            <div className="card ai-card">
              <div className="card-head">
                <span>AI 品質摘要</span>
                <button
                  className="btn primary xs"
                  onClick={() => void generateAiSummary()}
                  disabled={aiSummaryLoading || !visibleData}
                >
                  {aiSummaryLoading ? "產生中…" : "產生"}
                </button>
              </div>
              <div className="ai-body">
                {aiSummaryError && (
                  <span className="err-text tiny">{aiSummaryError}</span>
                )}
                {!aiSummaryError && aiSummary && <pre>{aiSummary}</pre>}
                {!aiSummaryError && !aiSummary && (
                  <span className="muted tiny">
                    尚未產生摘要。按「產生」以呼叫本機 Ollama。
                  </span>
                )}
              </div>
            </div>
          </section>
        </div>
      </div>
        </main>
      </div>

      {aiDrawerOpen && (
        <div className="drawer-mask" onClick={() => setAiDrawerOpen(false)}>
          <aside className="ai-drawer" onClick={(event) => event.stopPropagation()} aria-label="AI 品質摘要">
            <div className="drawer-head">
              <div><small>決策輔助</small><h2>AI 品質摘要</h2></div>
              <button className="modal-x" onClick={() => setAiDrawerOpen(false)} aria-label="關閉">×</button>
            </div>
            <div className="drawer-context">
              <b>{selFeature}</b>
              <span>{selProduct} · {selProcess} · {selMachine} · {selChartType}</span>
              <span>{currentPhase} · {selectedStatus} · Cpk {fmt(metrics?.cpk, 2)}</span>
            </div>
            <button className="btn primary drawer-generate" onClick={() => void generateAiSummary()} disabled={aiSummaryLoading || !visibleData}>
              {aiSummaryLoading ? "摘要產生中…" : aiSummary ? "重新產生摘要" : "產生 AI 摘要"}
            </button>
            <div className="drawer-summary">
              {aiSummaryError && <div className="flash err">{aiSummaryError}</div>}
              {!aiSummaryError && aiSummary && <pre>{aiSummary}</pre>}
              {!aiSummaryError && !aiSummary && <p className="muted">尚未產生摘要。系統會將目前選中尺寸的既有管制圖資料送至既有 AI summary API。</p>}
            </div>
          </aside>
        </div>
      )}

      {reportOpen && (
        <div className="modal-mask" onClick={() => setReportOpen(false)}>
          <div className="modal report-dialog" onClick={(event) => event.stopPropagation()}>
            <div className="modal-head"><h2>製程品質報表</h2><button className="modal-x" onClick={() => setReportOpen(false)}>×</button></div>
            <p className="muted">選擇要放入 A4 橫式報表的區塊。</p>
            {([
              ["charts", "管制圖"],
              ["capability", "製程能力指標"],
              ["abnormal", "異常判定與清單"],
              ["ai", "AI 品質摘要"],
            ] as Array<[keyof typeof reportOptions, string]>).map(([key, label]) => (
              <label className="report-option" key={key}>
                <input type="checkbox" checked={reportOptions[key]} onChange={() => toggleReportOption(key)} />
                <span>{label}</span>
              </label>
            ))}
            <div className="btn-row end"><button className="btn ghost" onClick={() => setReportOpen(false)}>取消</button><button className="btn primary" onClick={printReport} disabled={!Object.values(reportOptions).some(Boolean)}>預覽並列印</button></div>
          </div>
        </div>
      )}

      {reportPreview && (
        <section className="print-report">
          <header><div><h1>SPC 製程品質報表</h1><p>{selProduct} / {selProcess} / {selMachine} / {selFeature} / {selChartType}</p></div><div><b>{currentPhase}</b><p>產生時間 {new Date().toLocaleString("zh-TW")}</p></div></header>
          <div className="print-summary"><span>最新量測 <b>{latestPoint ? fmt(latestPoint.value) : "—"}</b></span><span>樣本 <b>{visibleData?.points.length ?? 0}</b></span><span>失控 <b>{stats.control}</b></span><span>超規 <b>{stats.spec}</b></span><span>Cpk <b>{fmt(metrics?.cpk, 2)}</b>{!hasActiveLimit && "（暫估）"}</span></div>
          {reportOptions.charts && visibleData && <div className="print-block"><h2>管制圖</h2><ControlChart data={visibleData} mode="monitor" height={225} />{visibleData.mr && <MovingRangeChart data={visibleData.mr} mode="monitor" height={190} />}</div>}
          {reportOptions.capability && <div className="print-block"><h2>製程能力{hasActiveLimit ? "" : "（Phase I 暫估）"}</h2><div className="print-metrics"><span>Cp {fmt(metrics?.cp, 2)}</span><span>Cpk {fmt(metrics?.cpk, 2)}</span><span>Ppk {fmt(metrics?.ppk, 2)}</span><span>平均 {fmt(metrics?.mean)}</span><span>σ {fmt(metrics?.sigma)}</span><span>USL {fmt(metrics?.usl)}</span><span>LSL {fmt(metrics?.lsl)}</span></div></div>}
          {reportOptions.abnormal && <div className="print-block"><h2>異常判定</h2><p>正常 {stats.normal} / 失控 {stats.control} / 超規 {stats.spec}</p>{abnormalCount === 0 ? <p>目前無異常點。</p> : <ul>{[...abnormalByChart.primary, ...abnormalByChart.secondary].slice(0, 20).map((item, index) => <li key={`${item.x}-${index}`}>{item.x} · {fmt(item.value)} · {item.violatedRules.map(ruleLabel).join("、")}</li>)}</ul>}</div>}
          {reportOptions.ai && <div className="print-block"><h2>AI 品質摘要</h2><pre>{aiSummary ?? "尚未產生 AI 摘要。"}</pre></div>}
          <button className="print-close no-print" onClick={() => setReportPreview(false)}>關閉報表預覽</button>
        </section>
      )}

      {/* ───────────── 設定 Modal ───────────── */}
      {settingsOpen && (
        <div className="modal-mask" onClick={() => setSettingsOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h2>設定</h2>
              <button
                className="modal-x"
                onClick={() => setSettingsOpen(false)}
                aria-label="關閉"
              >
                ×
              </button>
            </div>

            <label className="modal-field">
              <span>Phase I 建立管制線的最少樣本數</span>
              <input
                type="number"
                value={minSamplesDraft}
                min={configLimits.min}
                max={configLimits.max}
                onChange={(e) =>
                  setMinSamplesDraft(parseInt(e.target.value, 10) || 0)
                }
              />
              <small>
                允許 {configLimits.min}~{configLimits.max},目前 {minSamples}
              </small>
            </label>

            <label className="modal-field">
              <span>Cpk 良品判定門檻</span>
              <input
                type="number"
                step="0.01"
                value={cpkThresholdDraft}
                min={cpkLimits.min}
                max={cpkLimits.max}
                onChange={(e) =>
                  setCpkThresholdDraft(parseFloat(e.target.value) || 0)
                }
              />
              <small>
                允許 {cpkLimits.min}~{cpkLimits.max},目前 {cpkThreshold}
              </small>
            </label>

            <div className="modal-check">
              <label>
                <input
                  type="checkbox"
                  checked={autoCreateDraft}
                  onChange={(e) => setAutoCreateDraft(e.target.checked)}
                />
                <span>
                  <b>試算無異常時自動建立 active 管制線</b>
                  <small>
                    開啟後會批次檢查目前條件下所有球標尺寸；已有 active
                    者跳過，樣本不足或有疑似異常點者保留待人工確認。
                  </small>
                </span>
              </label>
            </div>

            <label className="modal-field">
              <span>密碼</span>
              <input
                type="password"
                value={settingsPassword}
                onChange={(e) => setSettingsPassword(e.target.value)}
                placeholder="需要密碼才能修改設定"
              />
            </label>

            {configMsg && (
              <div className={`flash ${configMsg.kind === "ok" ? "ok" : "err"}`}>
                {configMsg.text}
              </div>
            )}

            <div className="btn-row end">
              <button
                className="btn ghost"
                onClick={() => setSettingsOpen(false)}
              >
                取消
              </button>
              <button
                className="btn primary"
                onClick={() => void saveConfig()}
                disabled={savingConfig || autoBatch.running}
              >
                {savingConfig ? "儲存中…" : "儲存"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
