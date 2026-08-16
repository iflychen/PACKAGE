"use client";

import { useEffect, useState } from "react";
import {
  Brush,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { MrChartData, MrPoint } from "@/lib/types";

type ChartViewMode = "monitor" | "analysis";

const DEFAULT_WINDOW_SIZE = 40;

type VisibleRange = {
  start: number;
  end: number;
};

function round4(x: number): number {
  return Math.round(x * 10000) / 10000;
}

function defaultRange(count: number): VisibleRange {
  if (count <= 0) return { start: 0, end: 0 };
  const start = Math.max(0, count - DEFAULT_WINDOW_SIZE);
  return { start, end: count - 1 };
}

function clampRange(range: VisibleRange, count: number): VisibleRange {
  if (count <= 0) return { start: 0, end: 0 };
  const start = Math.max(0, Math.min(range.start, count - 1));
  const end = Math.max(start, Math.min(range.end, count - 1));
  return { start, end };
}

function renderDot(props: {
  cx?: number;
  cy?: number;
  index?: number;
  payload?: MrPoint;
}) {
  const { cx, cy, index, payload } = props;
  if (cx == null || cy == null || !payload || payload.value == null) {
    return <g key={`mrdot-empty-${index ?? 0}`} />;
  }

  const color = payload.is_out_of_control ? "#f59e0b" : "#16a34a";
  return (
    <circle
      key={`mrdot-${index ?? payload.x}`}
      cx={cx}
      cy={cy}
      r={payload.is_out_of_control ? 6 : 3.5}
      fill={color}
      stroke="#fff"
      strokeWidth={1.5}
    />
  );
}

function MrTooltip(props: {
  active?: boolean;
  payload?: Array<{ payload: MrPoint }>;
}) {
  const { active, payload } = props;
  if (!active || !payload || payload.length === 0) return null;

  const row = payload[0].payload;
  if (row.value == null) return null;
  return (
    <div className="tooltip">
      <div className="t-sn">{row.x}</div>
      <div>
        下圖值：<b>{row.value}</b>
      </div>
      <div style={{ marginTop: 4 }}>
        <span
          className="badge"
          style={{ background: row.is_out_of_control ? "#f59e0b" : "#16a34a" }}
        >
          {row.is_out_of_control ? "超出管制界線" : "正常"}
        </span>
      </div>
    </div>
  );
}

export default function MovingRangeChart({
  data,
  mode,
  height = 260,
}: {
  data: MrChartData;
  mode: ChartViewMode;
  height?: number;
}) {
  const { points, limits } = data;
  const [visibleRange, setVisibleRange] = useState<VisibleRange>(() =>
    defaultRange(points.length),
  );
  const [yZoom, setYZoom] = useState(1);

  const isAnalysisMode = mode === "analysis";

  useEffect(() => {
    setVisibleRange(defaultRange(points.length));
    setYZoom(1);
  }, [points.length, mode]);

  const safeRange = clampRange(visibleRange, points.length);
  const visiblePoints =
    points.length > 0 ? points.slice(safeRange.start, safeRange.end + 1) : points;
  const visibleCount = visiblePoints.length;
  const canZoom = points.length > 1;
  const domainPoints = isAnalysisMode ? visiblePoints : points;

  const setLatestWindow = (size: number) => {
    if (points.length <= 0) return;
    const start = Math.max(0, points.length - size);
    setVisibleRange({ start, end: points.length - 1 });
  };

  const showAll = () => {
    if (points.length <= 0) return;
    setVisibleRange({ start: 0, end: points.length - 1 });
  };

  const handleBrushChange = (range: {
    startIndex?: number;
    endIndex?: number;
  }) => {
    if (range.startIndex == null || range.endIndex == null) return;
    setVisibleRange(
      clampRange(
        { start: range.startIndex, end: range.endIndex },
        points.length,
      ),
    );
  };

  const zoomYIn = () => setYZoom((value) => Math.min(value * 1.25, 8));
  const zoomYOut = () => setYZoom((value) => Math.max(value / 1.25, 0.25));
  const resetYZoom = () => setYZoom(1);

  const vals = domainPoints
    .map((p) => p.value)
    .filter((v): v is number => v != null);
  const candidates = [...vals];
  if (limits) candidates.push(limits.ucl, limits.cl, limits.lcl);
  const max = candidates.length ? Math.max(...candidates) : 1;
  const baseMax = round4(max * 1.12 || 1);
  const domain: [number, number] = [
    0,
    round4(baseMax / (isAnalysisMode ? yZoom : 1)),
  ];

  const refLabel = (text: string, fill: string) => ({
    value: text,
    position: "right" as const,
    fill,
    fontSize: 11,
  });

  return (
    <>
      <div className="chart-zoom-controls">
        <div className="chart-zoom-status">
          {isAnalysisMode ? (
            <>
              顯示 {visibleCount > 0 ? safeRange.start + 1 : 0}-
              {visibleCount > 0 ? safeRange.end + 1 : 0} / {points.length} 筆
              <span className="chart-zoom-note">Y x{yZoom.toFixed(2)}</span>
            </>
          ) : (
            <>監管模式：全資料固定 Y 軸 / {points.length} 筆</>
          )}
        </div>
        {isAnalysisMode && (
          <div className="chart-zoom-actions">
            <button
              type="button"
              onClick={() => setLatestWindow(30)}
              disabled={!canZoom}
            >
              最近 30
            </button>
            <button
              type="button"
              onClick={() => setLatestWindow(60)}
              disabled={!canZoom}
            >
              最近 60
            </button>
            <button type="button" onClick={showAll} disabled={!canZoom}>
              全部
            </button>
            <button type="button" onClick={zoomYIn}>
              Y 放大
            </button>
            <button type="button" onClick={zoomYOut}>
              Y 縮小
            </button>
            <button type="button" onClick={resetYZoom}>
              Y 重置
            </button>
          </div>
        )}
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart
          data={points}
          margin={{ top: 10, right: 64, bottom: isAnalysisMode ? 56 : 28, left: 8 }}
        >
          <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
          <XAxis
            dataKey="x"
            tick={{ fontSize: 10 }}
            angle={-35}
            textAnchor="end"
            height={50}
            interval={0}
          />
          <YAxis
            domain={domain}
            width={64}
            tick={{ fontSize: 11 }}
            tickFormatter={(v: number) => v.toFixed(3)}
          />
          <Tooltip content={<MrTooltip />} />

          {limits && (
            <>
              <ReferenceLine
                y={limits.ucl}
                stroke="#f59e0b"
                strokeDasharray="4 4"
                label={refLabel(`UCL ${round4(limits.ucl)}`, "#d97706")}
              />
              <ReferenceLine
                y={limits.cl}
                stroke="#16a34a"
                label={refLabel(`CL ${round4(limits.cl)}`, "#15803d")}
              />
              <ReferenceLine
                y={limits.lcl}
                stroke="#94a3b8"
                strokeDasharray="4 4"
                label={refLabel(`LCL ${round4(limits.lcl)}`, "#64748b")}
              />
            </>
          )}

          <Line
            type="linear"
            dataKey="value"
            stroke="#475569"
            strokeWidth={1.5}
            connectNulls={false}
            dot={renderDot}
            activeDot={{ r: 7 }}
            isAnimationActive={false}
          />
          {isAnalysisMode && canZoom && (
            <Brush
              dataKey="x"
              height={26}
              travellerWidth={10}
              stroke="#2563eb"
              fill="#eff6ff"
              startIndex={safeRange.start}
              endIndex={safeRange.end}
              onChange={handleBrushChange}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </>
  );
}
