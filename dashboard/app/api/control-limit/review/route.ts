import { NextRequest, NextResponse } from "next/server";

import { saveTrialReview } from "@/lib/db";
import {
  validateReviewDisposition,
  type BaselineDisposition,
  type ExclusionReasonCode,
  type TrialReviewKey,
} from "@/lib/trialReview";
import type { ChartType } from "@/lib/types";

export const dynamic = "force-dynamic";

const CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];

function parseReviewRequest(body: unknown): {
  key: TrialReviewKey;
  point_id: string;
  baseline_disposition: BaselineDisposition;
  exclusion_reason_code: ExclusionReasonCode | null;
  exclusion_note: string | null;
  reviewed_by: string | null;
} {
  const value = body as Record<string, unknown>;
  const product = typeof value.product === "string" ? value.product.trim() : "";
  const process = typeof value.process === "string" ? value.process.trim() : "";
  const machine = typeof value.machine === "string" ? value.machine.trim() : "";
  const featureName =
    typeof value.feature_name === "string" ? value.feature_name.trim() : "";
  const chartType = value.chart_type as ChartType;
  const pointId =
    typeof value.point_id === "number" || typeof value.point_id === "string"
      ? String(value.point_id).trim()
      : "";
  if (
    !product ||
    !process ||
    !machine ||
    !featureName ||
    !pointId ||
    !CHART_TYPES.includes(chartType)
  ) {
    throw new Error(
      "需要完整的品號、製程、機台、尺寸、管制圖類型與量測點。",
    );
  }

  const validationError = validateReviewDisposition({
    baseline_disposition: value.baseline_disposition,
    exclusion_reason_code: value.exclusion_reason_code,
    exclusion_note: value.exclusion_note,
  });
  if (validationError) throw new Error(validationError);

  const rawInterval = value.event_interval_id;
  const eventIntervalId =
    typeof rawInterval === "number" && Number.isFinite(rawInterval)
      ? rawInterval
      : null;
  const disposition = value.baseline_disposition as BaselineDisposition;

  return {
    key: {
      product,
      process,
      machine,
      feature_name: featureName,
      chart_type: chartType,
      event_interval_id: eventIntervalId,
    },
    point_id: pointId,
    baseline_disposition: disposition,
    exclusion_reason_code:
      disposition === "exclude"
        ? (value.exclusion_reason_code as ExclusionReasonCode)
        : null,
    exclusion_note:
      disposition === "exclude" && typeof value.exclusion_note === "string"
        ? value.exclusion_note.trim() || null
        : null,
    reviewed_by:
      typeof value.reviewed_by === "string"
        ? value.reviewed_by.trim().slice(0, 120) || null
        : null,
  };
}

export async function POST(req: NextRequest) {
  try {
    const parsed = parseReviewRequest(await req.json());
    const review = await saveTrialReview(parsed);
    if (!review) {
      return NextResponse.json(
        { error: "找不到這個 Trial signal，請先重新執行 Phase I 試算。" },
        { status: 404 },
      );
    }
    return NextResponse.json({ ok: true, review });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 400 },
    );
  }
}
