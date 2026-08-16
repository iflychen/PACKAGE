"use client";

// 生產穩定度 — 每日 X-bar 折線
// 資料由 Next.js 端按日期 group 後算平均(不經 Python)。
// 只畫三條參考線:USL(紅)/ Standard 定義值(綠)/ LSL(紅)。

import { useMemo } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface DailyPoint {
  date: string;
  value: number;
  count: number;
}

interface Props {
  points: DailyPoint[];
  usl: number | null;
  lsl: number | null;
  standard: number | null;
  height?: number;
}

function round4(x: number): number {
  return Math.round(x * 10000) / 10000;
}

function formatLimit(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}

function DailyTooltip(props: {
  active?: boolean;
  payload?: Array<{ payload: DailyPoint }>;
}) {
  const { active, payload } = props;
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0].payload;
  return (
    <div className="tooltip">
      <div className="t-sn">{row.date}</div>
      <div>
        {row.count > 1 ? "平均" : "量測值"}：<b>{row.value}</b>
      </div>
      {row.count > 1 && <div className="muted">{row.count} 筆</div>}
    </div>
  );
}

export default function DailyTrendChart({
  points,
  usl,
  lsl,
  standard,
  height = 120,
}: Props) {
  // date 已經是後端算好的顯示標籤(依粒度為 MM/DD 或 MM/DD HH:MM)
  const rows = useMemo(
    () => points.map((p) => ({ ...p, label: p.date })),
    [points],
  );

  const domain = useMemo<[number, number]>(() => {
    const ys: number[] = rows
      .map((r) => r.value)
      .filter((v) => Number.isFinite(v));
    for (const v of [usl, lsl, standard]) {
      if (v != null && Number.isFinite(v)) ys.push(v);
    }
    if (ys.length === 0) return [0, 1];
    const min = Math.min(...ys);
    const max = Math.max(...ys);
    const pad = (max - min) * 0.12 || 0.05;
    return [round4(min - pad), round4(max + pad)];
  }, [rows, usl, lsl, standard]);

  if (rows.length === 0) {
    return <p className="muted tiny">尚無含量測時間的資料,無法繪製每日趨勢。</p>;
  }

  const refLabel = (text: string, fill: string) => ({
    value: text,
    position: "right" as const,
    fill,
    fontSize: 10,
  });

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart
        data={rows}
        margin={{ top: 6, right: 46, bottom: 2, left: 4 }}
      >
        <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
        <XAxis
          dataKey="label"
          tick={{ fontSize: 9 }}
          height={16}
          interval="preserveStartEnd"
          minTickGap={24}
        />
        <YAxis
          domain={domain}
          width={46}
          tick={{ fontSize: 9 }}
          tickFormatter={(v: number) => v.toFixed(2)}
        />
        <Tooltip content={<DailyTooltip />} />

        {usl != null && (
          <ReferenceLine
            y={usl}
            stroke="#dc2626"
            strokeDasharray="6 4"
            label={refLabel(`USL ${formatLimit(usl)}`, "#dc2626")}
          />
        )}
        {standard != null && (
          <ReferenceLine
            y={standard}
            stroke="#16a34a"
            strokeDasharray="4 4"
            label={refLabel(`Std ${formatLimit(standard)}`, "#15803d")}
          />
        )}
        {lsl != null && (
          <ReferenceLine
            y={lsl}
            stroke="#dc2626"
            strokeDasharray="6 4"
            label={refLabel(`LSL ${formatLimit(lsl)}`, "#dc2626")}
          />
        )}

        <Line
          type="linear"
          dataKey="value"
          stroke="#2563eb"
          strokeWidth={1.6}
          dot={{ r: 2, fill: "#2563eb", stroke: "#fff", strokeWidth: 1 }}
          activeDot={{ r: 5 }}
          isAnimationActive={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
