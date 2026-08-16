// GET  /api/config          → 讀目前設定 (不需密碼)
// POST /api/config           → 改設定 (三項皆為選擇性,有給才改)
//   body: { min_samples?, cpk_threshold?, auto_create_control_limit?, password }
//
// 密碼從環境變數 SETTINGS_PASSWORD 讀,沒設就用 "spc1234"。

import { NextRequest, NextResponse } from "next/server";
import {
  CONFIG_DEFAULTS,
  CONFIG_LIMITS,
  CPK_LIMITS,
  getAutoCreateControlLimit,
  getCpkThreshold,
  getMinSamples,
  setAutoCreateControlLimit,
  setCpkThreshold,
  setMinSamples,
} from "@/lib/config";

export const dynamic = "force-dynamic";

const DEFAULT_PASSWORD = "spc1234";

function getPassword(): string {
  return process.env.SETTINGS_PASSWORD || DEFAULT_PASSWORD;
}

function currentConfig() {
  return {
    min_samples: getMinSamples(),
    cpk_threshold: getCpkThreshold(),
    auto_create_control_limit: getAutoCreateControlLimit(),
    limits: CONFIG_LIMITS,
    cpk_limits: CPK_LIMITS,
    defaults: CONFIG_DEFAULTS,
  };
}

export async function GET() {
  return NextResponse.json(currentConfig());
}

interface PostBody {
  min_samples?: number;
  cpk_threshold?: number;
  auto_create_control_limit?: boolean;
  password?: string;
}

export async function POST(req: NextRequest) {
  let body: PostBody;
  try {
    body = (await req.json()) as PostBody;
  } catch {
    return NextResponse.json(
      { error: "request body 不是合法 JSON" },
      { status: 400 },
    );
  }

  // 密碼驗證
  if (!body.password || body.password !== getPassword()) {
    return NextResponse.json({ error: "密碼錯誤" }, { status: 401 });
  }

  const hasMinSamples = typeof body.min_samples === "number";
  const hasCpkThreshold = typeof body.cpk_threshold === "number";
  const hasAutoCreate = typeof body.auto_create_control_limit === "boolean";

  if (!hasMinSamples && !hasCpkThreshold && !hasAutoCreate) {
    return NextResponse.json(
      {
        error:
          "body 需要至少一項:min_samples(數字)、cpk_threshold(數字)、auto_create_control_limit(布林)",
      },
      { status: 400 },
    );
  }

  if (hasMinSamples) {
    const result = setMinSamples(body.min_samples as number);
    if (!result.ok) {
      return NextResponse.json(
        {
          error: `樣本數:${result.reason}`,
          current: result.value,
          limits: CONFIG_LIMITS,
        },
        { status: 400 },
      );
    }
  }

  if (hasCpkThreshold) {
    const result = setCpkThreshold(body.cpk_threshold as number);
    if (!result.ok) {
      return NextResponse.json(
        {
          error: `Cpk 門檻:${result.reason}`,
          current: result.value,
          cpk_limits: CPK_LIMITS,
        },
        { status: 400 },
      );
    }
  }

  if (hasAutoCreate) {
    setAutoCreateControlLimit(body.auto_create_control_limit as boolean);
  }

  return NextResponse.json({ ok: true, ...currentConfig() });
}
