import { Json } from "./api";

export const pct = (x: number | null | undefined, digits = 1) =>
  x === null || x === undefined ? "–" : `${(100 * x).toFixed(digits)}%`;

export const usd1M = (x: number | null | undefined) => (x === null || x === undefined ? "–" : `$${(1e6 * x).toFixed(1)}`);

export function when(ts?: string) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString();
}

export const TIER_NAMES: Record<string, string> = { "0": "rules", "1": "local", "2": "classifier", "3": "LLM", "4": "human" };

export function Meter({ value, bad }: { value: number; bad?: boolean }) {
  return (
    <div className={`meter${bad ? " bad" : ""}`} title={pct(value)}>
      <div style={{ width: `${Math.min(100, Math.max(0, 100 * value))}%` }} />
    </div>
  );
}

export function TierStack({ shares }: { shares: Record<string, number> }) {
  return (
    <div className="stack" title={Object.entries(shares).map(([t, v]) => `${TIER_NAMES[t] || t} ${pct(v)}`).join(", ")}>
      {Object.entries(shares)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([t, v]) => (
          <div key={t} className={`t${t}`} style={{ width: `${100 * v}%` }} />
        ))}
    </div>
  );
}

export function TierLegend() {
  return (
    <div className="legend">
      {Object.entries(TIER_NAMES).map(([t, n]) => (
        <span key={t}>
          <i className={`t${t}`} />
          {n}
        </span>
      ))}
    </div>
  );
}

export function Spark({ points }: { points: { n: number; flagged: number }[] }) {
  const max = Math.max(1e-9, ...points.map((p) => (p.n ? p.flagged / p.n : 0)));
  return (
    <div className="spark" title="flag rate over time">
      {points.map((p, i) => (
        <div key={i} style={{ height: `${Math.max(2, (100 * (p.n ? p.flagged / p.n : 0)) / max)}%` }} />
      ))}
    </div>
  );
}

export function ScoreBadge({ check, s }: { check: string; s: Json }) {
  const flagged = s.value === 1;
  return (
    <span className={`badge ${flagged ? "bad" : "ok"}`} title={`${s.evaluator?.id ?? s.evaluator} p=${s.p ?? ""}`}>
      {check}
      {s.label ? `: ${s.label}` : flagged ? "" : " ✓"}
    </span>
  );
}

export function ErrorLine({ error }: { error: string | null }) {
  return error ? <div className="err">{error}</div> : null;
}
