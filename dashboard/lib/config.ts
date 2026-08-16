// ============================================================================
//  Runtime 設定(in-memory)
//  ---------------------------------------------------------------------------
//  用途:讓前端能動態調整「Phase I 啟用管制線的最少樣本數」。
//  儲存位置:Next.js server process 記憶體。
//  伺服器重啟(npm run dev 停止再開)會回到預設值。
//
//  想要「重啟不掉」的話:可以改成寫到 .env / DB / 小檔案。目前為專題演示用途,
//  用記憶體最簡單。
// ============================================================================

const DEFAULT_MIN_SAMPLES = 5;
const HARD_MIN = 3; // Python 的下限
const HARD_MAX = 100;

let minSamples = DEFAULT_MIN_SAMPLES;

export function getMinSamples(): number {
  return minSamples;
}

export function setMinSamples(n: number): {
  ok: boolean;
  value: number;
  reason?: string;
} {
  if (!Number.isFinite(n)) {
    return { ok: false, value: minSamples, reason: "必須是數字" };
  }
  const rounded = Math.round(n);
  if (rounded < HARD_MIN) {
    return {
      ok: false,
      value: minSamples,
      reason: `不能小於 ${HARD_MIN}(Python 的計算下限)`,
    };
  }
  if (rounded > HARD_MAX) {
    return {
      ok: false,
      value: minSamples,
      reason: `不能大於 ${HARD_MAX}`,
    };
  }
  minSamples = rounded;
  return { ok: true, value: minSamples };
}

export const CONFIG_LIMITS = {
  min: HARD_MIN,
  max: HARD_MAX,
  default: DEFAULT_MIN_SAMPLES,
};

// ============================================================================
//  「試算無異常時自動建立 active 管制線」開關
//  ---------------------------------------------------------------------------
//  只在「首次試算 + 無排除點 + 上下兩張圖都沒有疑似異常點」時才會自動核准。
//  一旦使用者進入人工排除流程,仍必須手動按核准。
//  判斷邏輯在 app/api/control-limit/trial/route.ts,不信任前端狀態。
// ============================================================================

const DEFAULT_AUTO_CREATE_CONTROL_LIMIT = false;

let autoCreateControlLimit = DEFAULT_AUTO_CREATE_CONTROL_LIMIT;

export function getAutoCreateControlLimit(): boolean {
  return autoCreateControlLimit;
}

export function setAutoCreateControlLimit(value: boolean): boolean {
  autoCreateControlLimit = value;
  return autoCreateControlLimit;
}

export const CONFIG_DEFAULTS = {
  auto_create_control_limit: DEFAULT_AUTO_CREATE_CONTROL_LIMIT,
};

// ============================================================================
//  Cpk 良品判定門檻
//  ---------------------------------------------------------------------------
//  Cpk >= threshold        → 良品
//  Cpk <  threshold        → 不良
//  另外前端還會依 threshold 衍生四段配色(優良 / 良好 / 尚可 / 不良)。
// ============================================================================

const DEFAULT_CPK_THRESHOLD = 1.33;
const CPK_HARD_MIN = 0.5;
const CPK_HARD_MAX = 3;

let cpkThreshold = DEFAULT_CPK_THRESHOLD;

export function getCpkThreshold(): number {
  return cpkThreshold;
}

export function setCpkThreshold(n: number): {
  ok: boolean;
  value: number;
  reason?: string;
} {
  if (!Number.isFinite(n)) {
    return { ok: false, value: cpkThreshold, reason: "必須是數字" };
  }
  // 保留兩位小數
  const rounded = Math.round(n * 100) / 100;
  if (rounded < CPK_HARD_MIN) {
    return {
      ok: false,
      value: cpkThreshold,
      reason: `不能小於 ${CPK_HARD_MIN}`,
    };
  }
  if (rounded > CPK_HARD_MAX) {
    return {
      ok: false,
      value: cpkThreshold,
      reason: `不能大於 ${CPK_HARD_MAX}`,
    };
  }
  cpkThreshold = rounded;
  return { ok: true, value: cpkThreshold };
}

export const CPK_LIMITS = {
  min: CPK_HARD_MIN,
  max: CPK_HARD_MAX,
  default: DEFAULT_CPK_THRESHOLD,
};
