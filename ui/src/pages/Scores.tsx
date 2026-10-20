import { useState } from "react";
import { Link } from "react-router-dom";
import { Json, post } from "../api";
import { ErrorLine, Meter, pct, Spark, TierLegend, TierStack, when } from "../components";
import { useApi } from "../hooks";

// Screen 3: score explorer — rates per check, which layer decided, language slices, trend;
// suites with their active and shadow calibrations, drift alerts.
export function Scores() {
  const [days, setDays] = useState(7);
  const from = new Date(Date.now() - days * 86400e3).toISOString();
  const summary = useApi("/v1/scores/summary", { from });
  const suites = useApi("/v1/suites");
  const alerts = useApi("/v1/drift-alerts");
  const [msg, setMsg] = useState<string | null>(null);

  const drift = async (suite: string) => {
    setMsg(null);
    try {
      const r = await post(`/v1/suites/${suite}/drift-check`, undefined, { days, min_n: 30 });
      setMsg(`${suite}: ${r.alerts?.length ?? 0} new alert(s), ${r.disagreements ?? 0} audit disagreement(s), ${r.queued_for_annotation ?? 0} queued for annotation`);
      alerts.reload();
    } catch (e) {
      setMsg(String(e));
    }
  };

  const rows = Object.entries((summary.data || {}) as Record<string, Json>);
  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1>Scores &amp; calibration</h1>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="window">
          <option value={1}>last day</option>
          <option value={7}>last 7 days</option>
          <option value={30}>last 30 days</option>
        </select>
      </div>
      <ErrorLine error={summary.error} />
      <div className="panel table-wrap">
        <table>
          <thead>
            <tr>
              <th>check</th>
              <th className="num">decisions</th>
              <th>flag rate</th>
              <th>decided by</th>
              <th>by language</th>
              <th>trend</th>
              <th className="num">eval cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([check, r]) => (
              <tr key={check}>
                <td>
                  <Link to={`/?flag=${check}`}>{check}</Link>
                </td>
                <td className="num">{r.n}</td>
                <td>
                  <div className="row" style={{ gap: 6 }}>
                    <Meter value={r.rate} bad /> <span className="small">{pct(r.rate)}</span>
                  </div>
                </td>
                <td>
                  <TierStack shares={r.decided_by_tier} />
                  <div className="small muted">{Object.entries(r.decided_by as Record<string, number>).map(([e, v]) => `${e} ${pct(v, 0)}`).join(" · ")}</div>
                </td>
                <td className="small">
                  {Object.entries(r.by_lang as Record<string, Json>).map(([lg, v]) => (
                    <span key={lg} className="badge plain">
                      {lg} {pct(v.rate, 0)} <span className="muted">n={v.n}</span>
                    </span>
                  ))}
                </td>
                <td>
                  <Spark points={r.over_time} />
                </td>
                <td className="num small">${(r.cost_usd || 0).toFixed(4)}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">No scores in this window.</td>
              </tr>
            )}
          </tbody>
        </table>
        <TierLegend />
      </div>

      <h2>Suites</h2>
      <div className="panel table-wrap">
        <table>
          <thead>
            <tr>
              <th>suite</th>
              <th>agent</th>
              <th>active calibration</th>
              <th>shadow candidate</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(suites.data || []).map((s: Json) => (
              <tr key={s.id}>
                <td>{s.id}</td>
                <td className="small">{s.agent_name || "all agents"}</td>
                <td className="mono">{s.active ? <Link to={`/calibrations/${s.active}`}>{s.active}</Link> : "–"}</td>
                <td className="mono">{s.shadow ? <Link to={`/calibrations/${s.shadow}`}>{s.shadow}</Link> : "–"}</td>
                <td>
                  <button onClick={() => drift(s.id)}>Run drift check</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {msg && <div className="small" style={{ marginTop: 8 }}>{msg}</div>}
      </div>

      <h2>Drift alerts</h2>
      <div className="panel table-wrap">
        <table>
          <thead>
            <tr>
              <th>when</th>
              <th>check</th>
              <th>language</th>
              <th className="num">baseline</th>
              <th className="num">observed</th>
              <th className="num">n</th>
            </tr>
          </thead>
          <tbody>
            {(alerts.data || []).map((a: Json) => (
              <tr key={a.id}>
                <td className="small">{when(a.created_at)}</td>
                <td>{a.check_name}</td>
                <td>{a.lang || "all"}</td>
                <td className="num">{pct(a.baseline)}</td>
                <td className="num">
                  <span className="badge bad">{pct(a.observed)}</span>
                </td>
                <td className="num">{a.n}</td>
              </tr>
            ))}
            {(alerts.data || []).length === 0 && (
              <tr>
                <td colSpan={6} className="muted">No open drift alerts.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
