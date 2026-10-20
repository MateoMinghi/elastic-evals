import { useState } from "react";
import { useParams } from "react-router-dom";
import { Json, post } from "../api";
import { ErrorLine, pct, usd1M, when } from "../components";
import { useApi } from "../hooks";

// Screen 3 (detail): the calibration report — per check route, accuracy against its floor,
// share decided by each layer, language slices; the alternatives considered; lifecycle actions.
export function Calibration() {
  const { id = "" } = useParams();
  const { data, error, reload } = useApi(`/v1/calibrations/${id}`);
  const cmp = useApi(data?.status === "shadow" ? `/v1/suites/${data.suite_id}/shadow-comparison` : null);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <ErrorLine error={error} />;
  if (!data) return <div className="muted">loading…</div>;
  const rep = data.report;
  const act = async (action: string, force = false) => {
    setMsg(null);
    try {
      await post(`/v1/calibrations/${id}/${action}`, undefined, force ? { force: true } : undefined);
      reload();
    } catch (e) {
      setMsg(String(e));
    }
  };
  const langs: string[] = rep.languages || [];
  return (
    <>
      <h1 className="mono" style={{ fontSize: 16 }}>{id}</h1>
      <div className="row small" style={{ marginBottom: 12 }}>
        <span className={`badge ${data.status === "active" ? "ok" : data.status === "shadow" ? "warn" : "plain"}`}>{data.status}</span>
        <span className="muted">suite {data.suite_id}@{data.suite_version}</span>
        <span className="muted">{rep.n_labelled} labelled{rep.n_distilled ? ` + ${rep.n_distilled} distilled` : ""}</span>
        <span className="muted">created {when(data.created_at)}</span>
        <span className="muted">hosted set: {(data.hosted_set || []).join(", ") || "none (local-only)"}</span>
        <span className="muted">
          {usd1M(rep.suite_choice.usd_per_conv)} / 1M conv · {Math.round(rep.suite_choice.ms_per_conv)} ms / conv
        </span>
        {!rep.budget_met && <span className="badge bad">over budget</span>}
      </div>
      <div className="row" style={{ marginBottom: 14 }}>
        {data.status === "candidate" && <button onClick={() => act("shadow")}>Run in shadow</button>}
        {(data.status === "candidate" || data.status === "shadow") && (
          <>
            <button className="primary" onClick={() => act("promote")}>Promote</button>
            <button onClick={() => act("promote", true)}>Promote (force)</button>
            <button onClick={() => act("reject")}>Reject</button>
          </>
        )}
        {msg && <span className="err small">{msg}</span>}
      </div>
      <div className="panel table-wrap">
        <table>
          <thead>
            <tr>
              <th>check</th>
              <th>route</th>
              <th className="num">accuracy</th>
              <th className="num">floor</th>
              <th className="num">best</th>
              {langs.map((l) => (
                <th key={l} className="num">{l}</th>
              ))}
              <th>decided by</th>
              <th className="num">$ / 1M</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {Object.entries(rep.report as Record<string, Json>).map(([q, r]) => (
              <tr key={q}>
                <td>{q}</td>
                <td className="small">{rep.routes[q].join(" → ")}</td>
                <td className="num">{pct(r.chosen.accuracy)}</td>
                <td className="num muted">{pct(r.floor)}</td>
                <td className="num muted">{pct(r.best_accuracy)}</td>
                {langs.map((l) => (
                  <td key={l} className="num small">{pct(r.chosen.by_lang?.[l]?.accuracy)}</td>
                ))}
                <td className="small muted">
                  {Object.entries(r.chosen.decided_by as Record<string, number>).map(([e, v]) => `${e} ${pct(v, 0)}`).join(" · ")}
                </td>
                <td className="num small">{usd1M(r.chosen.usd_per_conv)}</td>
                <td>
                  {r.meets_floor === false ? <span className="badge bad">below floor</span> : r.meets_target ? <span className="badge ok">meets target</span> : <span className="badge warn">below target</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cmp.data && (
        <>
          <h2>Shadow comparison (agreement with the audit judge)</h2>
          <div className="panel table-wrap small">
            <table>
              <thead>
                <tr>
                  <th>check</th>
                  <th className="num">active</th>
                  <th className="num">candidate</th>
                  <th className="num">n</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries((cmp.data.checks || {}) as Record<string, Json>).map(([q, r]) => (
                  <tr key={q}>
                    <td>{q}</td>
                    <td className="num">{pct(r.active?.agreement)}</td>
                    <td className="num">{pct(r.candidate?.agreement)}</td>
                    <td className="num">{r.candidate?.n ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ marginTop: 8 }}>{cmp.data.promote ? <span className="badge ok">ready to promote</span> : <span className="badge warn">not yet: {cmp.data.status || (cmp.data.enough_data ? "candidate agrees less" : "not enough audited traffic")}</span>}</div>
          </div>
        </>
      )}
      <h2>Hosted sets considered</h2>
      <div className="panel table-wrap small">
        <table>
          <thead>
            <tr>
              <th>hosted set</th>
              <th className="num">checks below floor</th>
              <th className="num">$ / 1M conv</th>
              <th className="num">ms / conv</th>
            </tr>
          </thead>
          <tbody>
            {(rep.configs as Json[]).map((c, i) => (
              <tr key={i}>
                <td>{c.hosted.join(" + ") || "none"}</td>
                <td className="num">{c.short}</td>
                <td className="num">{usd1M(c.usd_per_conv)}</td>
                <td className="num">{Math.round(c.ms_per_conv)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
