"use client";

import { useEffect, useMemo, useState } from "react";
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

import type { BuildChartDataResponse } from "@/lib/types";
import {
  pointStatus,
  ruleLabel,
  STATUS_COLOR,
  STATUS_LABEL,
  type PointStatus,
} from "@/lib/labels";

type Row = BuildChartDataResponse["points"][number] & { status: PointStatus };
type ChartViewMode = "monitor" | "analysis";

const DEFAULT_WINDOW_SIZE = 40;

type VisibleRange = {
  start: number;
  end: number;
};

function round4(x: number): number {
  return Math.round(x * 10000) / 10000;
}

function formatLimit(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(4);
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
  payload?: Row;
}) {
  const { cx, cy, index, payload } = props;
  if (cx == null || cy == null || !payload) {
    return <g key={`dot-empty-${index ?? 0}`} />;
  }

  const color = STATUS_COLOR[payload.status];
  const abnormal = payload.status !== "normal";
  return (
    <circle
      key={`dot-${index ?? payload.x}`}
      cx={cx}
      cy={cy}
      r={abnormal ? 6 : 3.5}
      fill={color}
      stroke="#fff"
      strokeWidth={1.5}
    />
  );
}

function ChartTooltip(props: {
  active?: boolean;
  payload?: Array<{ payload: Row }>;
}) {
  const { active, payload } = props;
  if (!active || !payload || payload.length === 0) return null;

  const row = payload[0].payload;
  return (
    <div className="tooltip">
      <div className="t-sn">{row.x}</div>
      <div>
        量測值：<b>{row.value}</b>
      </div>
      {row.time && (
        <div className="muted">
          {row.time.replace("T", " ").replace("+08:00", "")}
        </div>
      )}
      <div style={{ marginTop: 4 }}>
        <span
          className="badge"
          style={{ background: STATUS_COLOR[row.status] }}
        >
          {STATUS_LABEL[row.status]}
        </span>
      </div>
      {row.violated_rules.length > 0 && (
        <div className="t-rule" style={{ marginTop: 4 }}>
          {row.violated_rules.map((r) => (
            <div key={r}>- {ruleLabel(r)}</div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ControlChart({
  data,
  mode,
  height = 430,
}: {
  data: BuildChartDataResponse;
  mode: ChartViewMode;
  height?: number;
}) {
  const { limits, points } = data;
  const rows: Row[] = useMemo(
    () => points.map((p) => ({ ...p, status: pointStatus(p) })),
    [points],
  );

  const [visibleRange, setVisibleRange] = useState<VisibleRange>(() =>
    defaultRange(rows.length),
  );
  const [yZoom, setYZoom] = useState(1);

  const isAnalysisMode = mode === "analysis";

  useEffect(() => {
    setVisibleRange(defaultRange(rows.length));
    setYZoom(1);
  }, [rows.length, mode]);

  const safeRange = clampRange(visibleRange, rows.length);
  const visibleRows =
    rows.length > 0 ? rows.slice(safeRange.start, safeRange.end + 1) : rows;
  const visibleCount = visibleRows.length;
  const canZoom = rows.length > 1;
  const domainRows = isAnalysisMode ? visibleRows : rows;

  const setLatestWindow = (size: number) => {
    if (rows.length <= 0) return;
    const start = Math.max(0, rows.length - size);
    setVisibleRange({ start, end: rows.length - 1 });
  };

  const showAll = () => {
    if (rows.length <= 0) return;
    setVisibleRange({ start: 0, end: rows.length - 1 });
  };

  const handleBrushChange = (range: {
    startIndex?: number;
    endIndex?: number;
  }) => {
    if (range.startIndex == null || range.endIndex == null) return;
    setVisibleRange(
      clampRange({ start: range.startIndex, end: range.endIndex }, rows.length),
    );
  };

  const zoomYIn = () => setYZoom((value) => Math.min(value * 1.25, 8));
  const zoomYOut = () => setYZoom((value) => Math.max(value / 1.25, 0.25));
  const resetYZoom = () => setYZoom(1);

  const processYs: number[] = domainRows
    .map((p) => p.value)
    .filter((value) => Number.isFinite(value));
  for (const value of [limits.ucl, limits.lcl, limits.cl]) {
    if (value != null) processYs.push(value);
  }

  const rawMin = processYs.length ? Math.min(...processYs) : 0;
  const rawMax = processYs.length ? Math.max(...processYs) : 1;
  const rawSpan = rawMax - rawMin || Math.max(Math.abs(rawMax) * 0.01, 0.001);
  const shouldIncludeSpecInScale = (value: number | null) =>
    value != null &&
    value >= rawMin - rawSpan * 3 &&
    value <= rawMax + rawSpan * 3;

  const ys = [...processYs];
  for (const value of [limits.usl, limits.lsl]) {
    if (value != null && shouldIncludeSpecInScale(value)) ys.push(value);
  }

  const min = ys.length ? Math.min(...ys) : 0;
  const max = ys.length ? Math.max(...ys) : 1;
  const pad = (max - min) * 0.12 || 0.1;
  const baseMin = min - pad;
  const baseMax = max + pad;
  const center = (baseMin + baseMax) / 2;
  const halfSpan = Math.max(
    (baseMax - baseMin) / 2 / (isAnalysisMode ? yZoom : 1),
    0.0005,
  );
  const domain: [number, number] = [
    round4(center - halfSpan),
    round4(center + halfSpan),
  ];

  const isInDomain = (value: number | null) =>
    value != null && value >= domain[0] && value <= domain[1];
  const hiddenSpecLimits = [
    limits.usl != null && !isInDomain(limits.usl)
      ? `USL ${formatLimit(limits.usl)}`
      : null,
    limits.lsl != null && !isInDomain(limits.lsl)
      ? `LSL ${formatLimit(limits.lsl)}`
      : null,
  ].filter((value): value is string => value != null);

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
              {visibleCount > 0 ? safeRange.end + 1 : 0} / {rows.length} 筆
              <span className="chart-zoom-note">Y x{yZoom.toFixed(2)}</span>
            </>
          ) : (
            <>監管模式：全資料固定 Y 軸 / {rows.length} 筆</>
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

      {hiddenSpecLimits.length > 0 && (
        <div
          className="chart-meta"
          style={{ marginBottom: 4, color: "#dc2626" }}
        >
          規格線超出目前縮放範圍：{hiddenSpecLimits.join("、")}
        </div>
      )}

      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart
          data={rows}
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
          <Tooltip content={<ChartTooltip />} />

          {limits.usl != null && isInDomain(limits.usl) && (
            <ReferenceLine
              y={limits.usl}
              stroke="#dc2626"
              strokeDasharray="6 4"
              label={refLabel(`USL ${formatLimit(limits.usl)}`, "#dc2626")}
            />
          )}
          {limits.lsl != null && isInDomain(limits.lsl) && (
            <ReferenceLine
              y={limits.lsl}
              stroke="#dc2626"
              strokeDasharray="6 4"
              label={refLabel(`LSL ${formatLimit(limits.lsl)}`, "#dc2626")}
            />
          )}
          {limits.ucl != null && (
            <ReferenceLine
              y={limits.ucl}
              stroke="#f59e0b"
              strokeDasharray="4 4"
              label={refLabel(`UCL ${formatLimit(limits.ucl)}`, "#d97706")}
            />
          )}
          {limits.lcl != null && (
            <ReferenceLine
              y={limits.lcl}
              stroke="#f59e0b"
              strokeDasharray="4 4"
              label={refLabel(`LCL ${formatLimit(limits.lcl)}`, "#d97706")}
            />
          )}
          {limits.cl != null && (
            <ReferenceLine
              y={limits.cl}
              stroke="#16a34a"
              label={refLabel(`CL ${formatLimit(limits.cl)}`, "#15803d")}
            />
          )}

          <Line
            type="linear"
            dataKey="value"
            stroke="#475569"
            strokeWidth={1.5}
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
