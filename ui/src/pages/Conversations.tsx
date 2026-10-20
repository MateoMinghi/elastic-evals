import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Json } from "../api";
import { ErrorLine, ScoreBadge, when } from "../components";
import { useApi } from "../hooks";

// Screen 2 (search) and the entry to screen 1 (viewer): full-text and filtered search over
// conversations, with facets from the traces-* rollups.
export function Conversations() {
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState(params.get("q") || "");
  const [cursor, setCursor] = useState<string | null>(null);
  const filters = {
    q: params.get("q") || undefined,
    flag: params.get("flag") || undefined,
    agent: params.get("agent") || undefined,
    lang: params.get("lang") || undefined,
    error: params.get("error") || undefined,
    experiment: params.get("experiment") || undefined,
    size: 50,
    search_after: cursor || undefined,
  };
  const { data, error, loading } = useApi("/v1/conversations", filters);
  const nav = useNavigate();

  const toggle = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (next.get(k) === v) next.delete(k);
    else next.set(k, v);
    setCursor(null);
    setParams(next);
  };

  return (
    <>
      <h1>Conversations</h1>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          const next = new URLSearchParams(params);
          if (q) next.set("q", q);
          else next.delete("q");
          setCursor(null);
          setParams(next);
        }}
      >
        <input type="search" placeholder="Search content…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="search" />
        <button className="primary" type="submit">Search</button>
        <label className="row small">
          <input type="checkbox" checked={params.get("error") === "true"} onChange={() => toggle("error", "true")} /> errors only
        </label>
        <span className="muted small">{data ? `${data.total} conversations` : loading ? "loading…" : ""}</span>
      </form>
      <ErrorLine error={error} />
      <div className="grid2" style={{ gridTemplateColumns: "minmax(0, 1fr) 220px", marginTop: 14 }}>
        <div className="panel table-wrap">
          <table>
            <thead>
              <tr>
                <th>started</th>
                <th>first message</th>
                <th>agent</th>
                <th>checks</th>
                <th className="num">turns</th>
                <th className="num">cost</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items || []).map((c: Json) => (
                <tr key={c.conversation_id} className="click" onClick={() => nav(`/conversations/${encodeURIComponent(c.conversation_id)}`)}>
                  <td className="small muted" style={{ whiteSpace: "nowrap" }}>{when(c.start_time)}</td>
                  <td>
                    {c.first_user_message?.slice(0, 120) || <span className="muted">{c.conversation_id}</span>}
                    {c.error && <span className="badge bad">error</span>}
                    {c.sentry_issue_ids?.length ? <span className="badge warn">sentry</span> : null}
                  </td>
                  <td className="small">{c.agent_name}</td>
                  <td>
                    {Object.entries(c.scores || {}).map(([k, s]) => (
                      <ScoreBadge key={k} check={k} s={s} />
                    ))}
                  </td>
                  <td className="num">{c.n_turns}</td>
                  <td className="num small">${(c.cost_usd || 0).toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data?.next && (
            <div className="row" style={{ marginTop: 10 }}>
              <button onClick={() => setCursor(data.next)}>Next page</button>
            </div>
          )}
        </div>
        <aside className="panel small">
          {(["flags", "agents", "langs"] as const).map((f) => {
            const key = f === "flags" ? "flag" : f === "agents" ? "agent" : "lang";
            const items = Object.entries((data?.facets?.[f] || {}) as Record<string, number>);
            return (
              <div key={f} style={{ marginBottom: 12 }}>
                <div className="muted" style={{ textTransform: "uppercase", fontSize: 11, marginBottom: 4 }}>{f}</div>
                {items.length === 0 && <div className="muted">–</div>}
                {items.map(([k, n]) => (
                  <div key={k} className={`facet${params.get(key) === k ? " on" : ""}`} onClick={() => toggle(key, k)}>
                    <span>{k}</span>
                    <span className="muted">{n}</span>
                  </div>
                ))}
              </div>
            );
          })}
        </aside>
      </div>
    </>
  );
}
