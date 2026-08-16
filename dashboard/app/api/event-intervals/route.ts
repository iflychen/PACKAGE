// GET /api/event-intervals?machine=...&event_type=...&product=...&process=...&feature=...
//
// 列出某機台在指定事件類型下的區間(原 /api/tool-intervals)。
//
// event_type 省略時退回預設「換刀」。事件類型必須帶下去:「事件使用區間」
// 的 LEAD() 是在同一事件類型內連續,不帶會把換刀和保養的區間混在一起。
//
// 帶了 product / process / feature 時,額外算出每個區間的:
//   - clean_sample_count  該區間內的乾淨樣本數
//   - control_start_time  累積到第 min_samples 筆乾淨樣本的量測時間
//                         (= 管制開始時間;不足則 null,代表仍在 Phase I)
//
// 該機台在該事件類型下沒有紀錄時回傳空陣列,前端會退回「不分區間」模式。

import { NextRequest, NextResponse } from "next/server";

import { getIntervalSampleStats, listEventIntervals } from "@/lib/db";
import { getMinSamples } from "@/lib/config";
import { DEFAULT_EVENT_TYPE } from "@/lib/types";

export const dynamic = "force-dynamic";

export interface EventIntervalOption {
  interval_id: number;
  serial_from: number;
  serial_to_exclusive: number | null;
  changed_at: string | null;
  clean_sample_count: number;
  control_start_time: string | null;
  first_measured_at: string | null;
  last_measured_at: string | null;
  /** 樣本已達門檻,可以(或已經)建立管制界線 */
  ready: boolean;
}

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const machine = sp.get("machine")?.trim();
  const product = sp.get("product")?.trim() || null;
  const process = sp.get("process")?.trim() || null;
  const feature = sp.get("feature")?.trim() || null;
  const eventType = sp.get("event_type")?.trim() || DEFAULT_EVENT_TYPE;

  if (!machine) {
    return NextResponse.json({ error: "缺少參數 machine" }, { status: 400 });
  }

  const minSamples = getMinSamples();

  let intervals;
  try {
    intervals = await listEventIntervals(machine, eventType);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "讀取事件使用區間失敗", detail },
      { status: 500 },
    );
  }

  if (intervals.length === 0) {
    return NextResponse.json({
      ok: true,
      machine,
      event_type: eventType,
      min_samples: minSamples,
      intervals: [] as EventIntervalOption[],
    });
  }

  // 沒指定尺寸就只回區間本身,不算樣本累積
  if (!product || !process || !feature) {
    return NextResponse.json({
      ok: true,
      machine,
      event_type: eventType,
      min_samples: minSamples,
      intervals: intervals.map(
        (it): EventIntervalOption => ({
          interval_id: it.interval_id,
          serial_from: it.serial_from,
          serial_to_exclusive: it.serial_to_exclusive,
          changed_at: it.changed_at,
          clean_sample_count: 0,
          control_start_time: null,
          first_measured_at: null,
          last_measured_at: null,
          ready: false,
        }),
      ),
    });
  }

  let stats;
  try {
    stats = await getIntervalSampleStats(
      product,
      process,
      machine,
      feature,
      minSamples,
      eventType,
    );
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "計算區間樣本累積失敗", detail },
      { status: 500 },
    );
  }

  const byId = new Map(stats.map((s) => [s.interval_id, s]));

  const options: EventIntervalOption[] = intervals.map((it) => {
    const s = byId.get(it.interval_id);
    return {
      interval_id: it.interval_id,
      serial_from: it.serial_from,
      serial_to_exclusive: it.serial_to_exclusive,
      changed_at: it.changed_at,
      clean_sample_count: s?.clean_sample_count ?? 0,
      control_start_time: s?.control_start_time ?? null,
      first_measured_at: s?.first_measured_at ?? null,
      last_measured_at: s?.last_measured_at ?? null,
      ready: (s?.clean_sample_count ?? 0) >= minSamples,
    };
  });

  return NextResponse.json({
    ok: true,
    machine,
    event_type: eventType,
    product,
    process,
    feature,
    min_samples: minSamples,
    intervals: options,
  });
}
