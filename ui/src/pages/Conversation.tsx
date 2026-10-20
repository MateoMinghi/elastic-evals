import { Link, useParams } from "react-router-dom";
import { Json } from "../api";
import { ErrorLine, pct, TIER_NAMES, when } from "../components";
import { useApi } from "../hooks";

function nanos(ts: string) {
  const m = /^(.*?)(?:\.(\d+))?Z$/.exec(ts);
  const base = Date.parse((m ? m[1] : ts) + "Z");
  const frac = m && m[2] ? Number((m[2] + "000000000").slice(0, 9)) : 0;
  return base * 1e6 + frac;
}

function Timeline({ spans }: { spans: Json[] }) {
  if (!spans.length) return null;
  const t0 = Math.min(...spans.map((s) => nanos(s.start_time)));
  const t1 = Math.max(...spans.map((s) => nanos(s.end_time || s.start_time)));
  const total = Math.max(1, t1 - t0);
  const children: Record<string, Json[]> = {};
  const ids = new Set(spans.map((s) => s.span_id));
  for (const s of spans) (children[ids.has(s.parent_span_id) ? s.parent_span_id : "root"] ||= []).push(s);
  const rows: { s: Json; depth: number }[] = [];
  const walk = (key: string, depth: number) => {
    for (const s of (children[key] || []).sort((a, b) => nanos(a.start_time) - nanos(b.start_time))) {
      rows.push({ s, depth });
      walk(s.span_id, depth + 1);
    }
  };
  walk("root", 0);
  return (
    <div>
      {rows.map(({ s, depth }) => {
        const left = (100 * (nanos(s.start_time) - t0)) / total;
        const width = Math.max(0.5, (100 * (nanos(s.end_time || s.start_time) - nanos(s.start_time))) / total);
        return (
          <div className="span-row" key={s.span_id} title={s.input_preview || s.output_preview || s.name}>
            <div className="span-name" style={{ paddingLeft: depth * 12 }}>
              {s.name} {s.status === "ERROR" && <span className="badge bad">error</span>}
            </div>
            <div className="track">
              <div className={`bar ${s.status === "ERROR" ? "ERROR" : s.type}`} style={{ left: `${left}%`, width: `${width}%` }} />
            </div>
            <div className="small muted num">{Math.round(s.duration_ms ?? 0)} ms</div>
          </div>
        );
      })}
    </div>
  );
}

// Screen 1: the conversation viewer — turns, span timeline, every score with its route, layer
// and probability (shadow and audit scores alongside), Sentry links, similar conversations.
export function Conversation() {
  const { id = "" } = useParams();
  const { data, error } = useApi(`/v1/conversations/${encodeURIComponent(id)}`);
  const similar = useApi(`/v1/conversations/${encodeURIComponent(id)}/similar`, { k: 5 });
  if (error) return <ErrorLine error={error} />;
  if (!data) return <div className="muted">loading…</div>;
  const conv = data.conversation;
  const decided = (data.scores as Json[]).filter((s) => s.decision);
  const other = (data.scores as Json[]).filter((s) => !s.decision);
  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 className="mono" style={{ fontSize: 16 }}>{id}</h1>
        <Link to="/">← all conversations</Link>
      </div>
      <div className="row small muted" style={{ marginBottom: 12 }}>
        <span>{when(data.rollup?.start_time)}</span>
        <span>agent {data.rollup?.agent_name}</span>
        <span>lang {conv?.lang}</span>
        <span>{data.rollup?.n_llm_calls} LLM calls · {data.rollup?.n_tool_calls} tool calls</span>
        <span>{data.rollup?.content_kept ? `content kept (${(data.rollup?.retention_reasons || []).join(", ")})` : "content not kept"}</span>
        {data.rollup?.calibration_id && (
          <span>
            calibration <Link to={`/calibrations/${data.rollup.calibration_id}`}>{data.rollup.calibration_id}</Link>
          </span>
        )}
      </div>
      <div className="grid2">
        <div>
          <div className="panel">
            {conv?.record && Object.keys(conv.record).length > 0 && (
              <div className="small mono muted" style={{ marginBottom: 8 }}>record {JSON.stringify(conv.record)}</div>
            )}
            {(conv?.turns || []).map((t: Json, i: number) => (
              <div key={i} className={`turn ${t.role}`}>
                {t.role === "tool" ? (
                  <>
                    {t.name}({JSON.stringify(t.args)}) → {t.result}
                  </>
                ) : (
                  <>
                    <div className="who">{t.role}</div>
                    {t.text}
                  </>
                )}
              </div>
            ))}
            {!conv && <div className="muted">Content not available (not kept by retention, or expired).</div>}
          </div>
          <h2>Spans</h2>
          <div className="panel">
            <Timeline spans={data.spans} />
          </div>
        </div>
        <div>
          <div className="panel">
            <table>
              <thead>
                <tr>
                  <th>check</th>
                  <th>result</th>
                  <th>decided by</th>
                  <th className="num">p</th>
                </tr>
              </thead>
              <tbody>
                {decided.map((s) => (
                  <tr key={s.id}>
                    <td>{s.check}</td>
                    <td>
                      <span className={`badge ${s.value === 1 ? "bad" : "ok"}`}>{s.label ?? (s.value === 1 ? "flagged" : "ok")}</span>
                    </td>
                    <td className="small">
                      {s.evaluator.id} <span className="muted">({TIER_NAMES[String(s.evaluator.tier)]})</span>
                      <div className="muted">route {(s.route || []).join(" → ")}</div>
                    </td>
                    <td className="num small">{s.p === null || s.p === undefined ? "–" : s.p.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {other.length > 0 && (
              <>
                <h2>Shadow, audit and human scores</h2>
                <table>
                  <tbody>
                    {other.map((s) => (
                      <tr key={s.id}>
                        <td>{s.check}</td>
                        <td>
                          <span className="badge plain">{s.sample_reason}</span>
                        </td>
                        <td className="small">{s.evaluator.id}</td>
                        <td className="num small">{s.label ?? s.value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
          <h2>Sentry</h2>
          <div className="panel small">
            {(data.sentry as Json[]).length === 0 && <span className="muted">No linked issues.</span>}
            {(data.sentry as Json[]).map((l) => (
              <div key={`${l.trace_id}-${l.sentry_issue_id ?? l.id}`}>
                issue {l.sentry_issue_id ?? l.id} <span className="badge plain">{l.status}</span>{" "}
                {l.permalink && <a href={l.permalink}>open</a>}
              </div>
            ))}
          </div>
          <h2>Similar conversations</h2>
          <div className="panel small">
            {similar.error && <span className="muted">No embedding for this conversation.</span>}
            {(similar.data || []).map((s: Json) => (
              <div key={s.conversation_id} className="row" style={{ justifyContent: "space-between" }}>
                <Link to={`/conversations/${encodeURIComponent(s.conversation_id)}`}>{s.conversation_id}</Link>
                <span className="muted">
                  {(s.flags || []).join(", ")} {pct(s.score, 0)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}
