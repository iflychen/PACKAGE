// GET /api/capability?product=...&process=...&machine=...&feature=...
//
// 儀表板「② 製程能力」區塊的資料來源,一次回傳:
//   1) table     — 該機台底下「所有球標尺寸」的規格 + Cpk(CPK 統計表)
//   2) metrics   — 目前選中尺寸的完整能力指標(Cpk/Cp/Ppk/Cpm/Cpmk/X-bar/σ/USL/LSL)
//   3) daily     — 每日 X-bar 折線 + 量測天數 + 最後量測時間
//   4) persisted — 能力值回寫「管制圖」的結果統計
//
// Python 端只提供單筆 /spc/capability,所以這裡對每個尺寸並行呼叫。
// feature 省略時只回 table(不算 metrics / daily)。
//
// 回寫規則:算完後把 cp/cpk/cpm/cpmk/ppk 更新到「管制圖」目前生效的版本,
// 讓 DB 的能力欄位跟畫面一致。只寫能力欄位,不動六條管制界線;
// 仍在 Phase I(沒有 active 版本)的尺寸會被跳過。

import { NextRequest, NextResponse } from "next/server";

import { DEFAULT_EVENT_TYPE } from "@/lib/types";

import {
  getDailySummary,
  listFeatureSpecsWithValues,
  updateCapabilityValues,
} from "@/lib/db";
import { calculateCapabilityDetail, getSpcApiBase } from "@/lib/spcClient";
import { getCpkThreshold } from "@/lib/config";
import type { ChartType } from "@/lib/types";

export const dynamic = "force-dynamic";

const CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];

// 同時打 Python 的最大並行數,避免一次噴太多連線
const CONCURRENCY = 6;

export interface CapabilityTableRow {
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

async function mapWithLimit<T, R>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let cursor = 0;

  const workers = Array.from(
    { length: Math.min(limit, items.length) },
    async () => {
      for (;;) {
        const index = cursor++;
        if (index >= items.length) return;
        results[index] = await fn(items[index], index);
      }
    },
  );

  await Promise.all(workers);
  return results;
}

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const product = sp.get("product")?.trim();
  const process = sp.get("process")?.trim();
  const machine = sp.get("machine")?.trim();
  const feature = sp.get("feature")?.trim() || null;
  // 事件區間:有帶就只用該區間內的樣本算能力
  const intervalRaw = sp.get("event_interval_id")?.trim();
  const eventIntervalId =
    intervalRaw && Number.isFinite(Number(intervalRaw))
      ? Number(intervalRaw)
      : null;
  const eventType = sp.get("event_type")?.trim() || DEFAULT_EVENT_TYPE;

  if (!product || !process || !machine) {
    return NextResponse.json(
      { error: "缺少參數,需要 product / process / machine" },
      { status: 400 },
    );
  }

  // 1) DB:全尺寸規格 + 量測值
  let features;
  try {
    features = await listFeatureSpecsWithValues(
      product,
      process,
      machine,
      eventIntervalId,
      eventType,
    );
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "連接資料庫來源錯誤", detail },
      { status: 500 },
    );
  }

  // 2) Python:每個尺寸算一次能力
  const details = await mapWithLimit(features, CONCURRENCY, async (f) => {
    if (f.values.length < 2) {
      return { feature: f, detail: null, error: "樣本不足(需至少 2 筆)" };
    }
    try {
      const detail = await calculateCapabilityDetail(
        `${product}::${process}::${machine}::${f.feature_name}`,
        f.feature_name,
        f.spec,
        f.values,
      );
      return { feature: f, detail, error: undefined as string | undefined };
    } catch (err) {
      return {
        feature: f,
        detail: null,
        error: err instanceof Error ? err.message : String(err),
      };
    }
  });

  const table: CapabilityTableRow[] = details.map((d, i) => ({
    index: i + 1,
    feature_name: d.feature.feature_name,
    nominal_value: d.feature.spec.nominal_value,
    upper_tolerance: d.feature.spec.upper_tolerance,
    lower_tolerance: d.feature.spec.lower_tolerance,
    usl: d.detail ? d.detail.usl : null,
    lsl: d.detail ? d.detail.lsl : null,
    cpk: d.detail ? d.detail.cpk : null,
    sample_size: d.feature.sample_size,
    error: d.error,
  }));

  // Python 全掛掉才視為服務不可用
  const allPythonFailed =
    details.length > 0 &&
    details.every((d) => d.detail === null && d.error != null) &&
    features.some((f) => f.values.length >= 2);

  if (allPythonFailed) {
    return NextResponse.json(
      {
        error: "無法連線 Python SPC 服務,請確認 uvicorn 有在執行。",
        spc_api_base: getSpcApiBase(),
        detail: details[0]?.error,
      },
      { status: 502 },
    );
  }

  // 3) 回寫:把重算的能力值更新到「管制圖」目前生效的版本
  //    只有已進入 Phase II(有 active 版本)的尺寸會被更新;
  //    寫入失敗不影響本次回傳,只記在 persisted.errors。
  const persisted = { updated: 0, skipped: 0, errors: [] as string[] };

  await mapWithLimit(details, CONCURRENCY, async (d) => {
    if (!d.detail) {
      persisted.skipped += 1;
      return;
    }
    try {
      // 能力值和圖類型無關(都是同一批樣本算的),但每個 管制圖類型 × 區間
      // 是各自獨立的一列,所以三種圖類型都要各更新一次。
      // 不加 管制圖類型 條件的話,I-MR 的值會蓋到 Xbar-R / Xbar-S 上。
      let n = 0;
      for (const ct of CHART_TYPES) {
        n += await updateCapabilityValues(
          product,
          process,
          machine,
          d.feature.feature_name,
          ct,
          {
            cp: d.detail.cp,
            cpk: d.detail.cpk,
            cpm: d.detail.cpm,
            cpmk: d.detail.cpmk,
            ppk: d.detail.ppk,
          },
          eventIntervalId,
          eventType,
        );
      }
      if (n > 0) persisted.updated += n;
      else persisted.skipped += 1; // 仍在 Phase I,沒有 active 版本
    } catch (err) {
      persisted.errors.push(
        `${d.feature.feature_name}: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  });

  // 4) 選中尺寸的 metrics + 每日折線
  let metrics = null;
  let daily = null;

  if (feature) {
    const hit = details.find((d) => d.feature.feature_name === feature);
    if (hit?.detail) {
      metrics = {
        feature_name: feature,
        cpk: hit.detail.cpk,
        cp: hit.detail.cp,
        cpm: hit.detail.cpm,
        cpmk: hit.detail.cpmk,
        ppk: hit.detail.ppk,
        mean: hit.detail.mean,
        sigma: hit.detail.sigma,
        usl: hit.detail.usl,
        lsl: hit.detail.lsl,
        nominal_value: hit.feature.spec.nominal_value,
        sample_size: hit.detail.sample_size,
      };
    }

    try {
      daily = await getDailySummary(
        product,
        process,
        machine,
        feature,
        eventIntervalId,
        eventType,
      );
    } catch {
      daily = null;
    }
  }

  return NextResponse.json({
    ok: true,
    product,
    process,
    machine,
    feature,
    event_interval_id: eventIntervalId,
    event_type: eventType,
    cpk_threshold: getCpkThreshold(),
    table,
    metrics,
    daily,
    persisted,
  });
}
