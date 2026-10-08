import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const SPC_API_BASE = process.env.SPC_API_BASE ?? "http://127.0.0.1:8000";

interface AiSummaryBody {
  chartData?: unknown;
  /** ② 製程能力的資料；Python 端以 capability_data 接收，可省略。 */
  capabilityData?: unknown;
}

export async function POST(req: NextRequest) {
  let body: AiSummaryBody;
  try {
    body = (await req.json()) as AiSummaryBody;
  } catch {
    return NextResponse.json(
      { error: "request body must be valid JSON" },
      { status: 400 },
    );
  }

  if (!body.chartData || typeof body.chartData !== "object") {
    return NextResponse.json(
      { error: "chartData is required" },
      { status: 400 },
    );
  }

  try {
    const res = await fetch(`${SPC_API_BASE}/spc/ai-summary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chart_data: body.chartData,
        capability_data:
          body.capabilityData && typeof body.capabilityData === "object"
            ? body.capabilityData
            : null,
      }),
      cache: "no-store",
    });

    const text = await res.text();
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(text) as Record<string, unknown>;
    } catch {
      payload = { detail: text };
    }

    if (!res.ok) {
      return NextResponse.json(
        {
          error: "Python SPC AI summary failed",
          detail: payload.detail ?? payload.error ?? String(payload),
          spc_api_base: SPC_API_BASE,
        },
        { status: 502 },
      );
    }

    return NextResponse.json(payload);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      {
        error: "Cannot reach Python SPC AI summary service",
        detail,
        spc_api_base: SPC_API_BASE,
      },
      { status: 502 },
    );
  }
}
