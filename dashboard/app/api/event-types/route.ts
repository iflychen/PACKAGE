// GET /api/event-types?machine=...
//
// 列出「事件紀錄」裡實際出現過的事件類型,給①區的事件類型下拉用。
//
// 事件類型是自由文字(換刀 / 保養 / 參數調整 …),由使用者自己填,
// 所以不能在前端寫死選項,一定要從 DB 撈。
//
// 帶 machine 時只回該機台有的類型;省略則回全部。
// DB 完全沒有事件紀錄時回傳空陣列 —— 前端會退回「不分區間」模式。

import { NextRequest, NextResponse } from "next/server";

import { listEventTypes } from "@/lib/db";
import { DEFAULT_EVENT_TYPE } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const machine = req.nextUrl.searchParams.get("machine")?.trim() || null;

  try {
    const types = await listEventTypes(machine);
    return NextResponse.json({
      ok: true,
      machine,
      // 預設值只在清單裡真的有它時才回,避免前端選到一個 DB 不存在的類型
      // 而查出零筆區間。
      default_event_type: types.includes(DEFAULT_EVENT_TYPE)
        ? DEFAULT_EVENT_TYPE
        : (types[0] ?? null),
      event_types: types,
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "讀取事件類型失敗", detail },
      { status: 500 },
    );
  }
}
