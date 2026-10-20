import { useState } from "react";
import { Link } from "react-router-dom";
import { Json } from "../api";
import { ErrorLine, pct, when } from "../components";
import { useApi } from "../hooks";

// Screen 4: experiment comparison — datasets, runs with their gates, two runs side by side.
export function Experiments() {
  const datasets = useApi("/v1/datasets");
  const [dataset, setDataset] = useState<string>("");
  const runs = useApi("/v1/experiments", { dataset_id: dataset || undefined });
  const [pick, setPick] = useState<string[]>([]);
  const cmp = useApi(pick.length === 2 ? "/v1/experiments/compare" : null, { a: pick[0], b: pick[1] });
  const toggle = (id: string) => setPick((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id].slice(-2)));
  return (
    <>
      <h1>Experiments</h1>
      <div className="row" style={{ marginBottom: 12 }}>
        <select value={dataset} onChange={(e) => { setDataset(e.target.value); setPick([]); }} aria-label="dataset">
          <option value="">all datasets</option>
          {(datasets.data || []).map((d: Json) => (
            <option key={d.id} value={d.id}>
              {d.name} ({d.items} items)
            </option>
          ))}
        </select>
        <span className="muted small">Select two runs to compare them.</span>
      </div>
      <ErrorLine error={runs.error} />
      <div className="panel table-wrap">
        <table>
          <thead>
            <tr>
              <th />
              <th>run</th>
              <th>when</th>
              <th>gates</th>
              <th>flag rates</th>
            </tr>
          </thead>
          <tbody>
            {(runs.data || []).map((r: Json) => (
              <tr key={r.id} className="click" onClick={() => toggle(r.id)}>
                <td>
                  <input type="checkbox" readOnly checked={pick.includes(r.id)} aria-label={`select ${r.name}`} />
                </td>
                <td>
                  {r.name} <div className="mono small muted">{r.id}</div>
                </td>
                <td className="small">{when(r.created_at)}</td>
                <td>
                  <span className={`badge ${r.status === "passed" ? "ok" : "bad"}`}>{r.status}</span>
                  {(r.summary?.gates || []).filter((g: Json) => !g.pass).map((g: Json) => (
                    <span key={g.check} className="badge bad">{g.check}</span>
                  ))}
                </td>
                <td className="small">
                  {Object.entries((r.summary?.rates || {}) as Record<string, number | null>)
                    .filter(([, v]) => v !== null)
                    .map(([k, v]) => `${k} ${pct(v, 0)}`)
                    .join(" · ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cmp.data && (
        <>
          <h2>Comparison</h2>
          <div className="panel table-wrap">
            <table>
              <thead>
                <tr>
                  <th>check</th>
                  <th className="num">A</th>
                  <th className="num">B</th>
                  <th className="num">Δ</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(cmp.data.rates as Record<string, [number | null, number | null]>).map(([k, [a, b]]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td className="num">{pct(a)}</td>
                    <td className="num">{pct(b)}</td>
                    <td className="num">
                      {a !== null && b !== null ? <span className={`badge ${b > a ? "bad" : b < a ? "ok" : "plain"}`}>{((b - a) * 100).toFixed(1)} pts</span> : "–"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <h2>Items that changed ({cmp.data.changed_items.length})</h2>
            <table>
              <tbody>
                {cmp.data.changed_items.map((it: Json) => (
                  <tr key={it.item_id}>
                    <td className="mono small">
                      <Link to={`/conversations/${encodeURIComponent(it.item_id)}`}>{it.item_id}</Link>
                    </td>
                    <td className="small">
                      {Object.entries(it.changed as Record<string, [unknown, unknown]>).map(([k, [a, b]]) => (
                        <span key={k} className="badge warn">
                          {k}: {String(a)} → {String(b)}
                        </span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
