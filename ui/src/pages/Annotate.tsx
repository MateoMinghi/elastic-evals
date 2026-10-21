import { useCallback, useEffect, useState } from "react";
import { Json, post } from "../api";
import { ErrorLine } from "../components";
import { useApi } from "../hooks";

// Screen 5: keyboard-driven annotation. 1 / 0 answer the focused check, j / k move between
// checks, Enter submits, s skips. Labels go to the queue's label set and feed calibration.
export function Annotate() {
  const queues = useApi("/v1/annotation-queues");
  const [qid, setQid] = useState<string>("");
  const [item, setItem] = useState<Json | null>(null);
  const [labels, setLabels] = useState<Record<string, number>>({});
  const [focus, setFocus] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(0);
  const queue = (queues.data || []).find((q: Json) => q.id === qid);
  const checks: string[] = queue?.checks || [];

  const next = useCallback(async () => {
    setError(null);
    try {
      const r = await post(`/v1/annotation-queues/${qid}/next`);
      setItem(r.item ? r : null);
      setLabels({});
      setFocus(0);
      if (!r.item) setError("Queue is empty.");
    } catch (e) {
      setError(String(e));
    }
  }, [qid]);

  const submit = useCallback(async () => {
    if (!item) return;
    if (checks.some((c) => labels[c] === undefined)) {
      setError("Answer every check first.");
      return;
    }
    try {
      await post(`/v1/annotation-queues/${qid}/items/${encodeURIComponent(item.item.conversation_id)}/labels`, {
        labels, lang: item.conversation?.lang,
      });
      setDone((d) => d + 1);
      queues.reload(); // keep the pending count in the header current
      next();
    } catch (e) {
      setError(String(e));
    }
  }, [item, labels, checks, qid, next, queues]);

  const skip = useCallback(async () => {
    if (!item) return;
    await post(`/v1/annotation-queues/${qid}/items/${encodeURIComponent(item.item.conversation_id)}/skip`);
    next();
  }, [item, qid, next]);

  useEffect(() => {
    if (!item) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).tagName === "INPUT") return;
      if (e.key === "1" || e.key === "0") {
        setLabels((l) => ({ ...l, [checks[focus]]: Number(e.key) }));
        setFocus((f) => Math.min(checks.length - 1, f + 1));
      } else if (e.key === "j") setFocus((f) => Math.min(checks.length - 1, f + 1));
      else if (e.key === "k") setFocus((f) => Math.max(0, f - 1));
      else if (e.key === "Enter") submit();
      else if (e.key === "s") skip();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [item, checks, focus, submit, skip]);

  return (
    <>
      <h1>Annotation</h1>
      <div className="row" style={{ marginBottom: 12 }}>
        <select value={qid} onChange={(e) => { setQid(e.target.value); setItem(null); }} aria-label="queue">
          <option value="">choose a queue…</option>
          {(queues.data || []).map((q: Json) => (
            <option key={q.id} value={q.id}>
              {q.name} — {q.counts?.pending ?? 0} pending
            </option>
          ))}
        </select>
        <button className="primary" disabled={!qid} onClick={next}>Start</button>
        <span className="small muted">
          <span className="kbd">1</span>/<span className="kbd">0</span> answer · <span className="kbd">j</span>/<span className="kbd">k</span> move ·{" "}
          <span className="kbd">Enter</span> submit · <span className="kbd">s</span> skip · {done} labelled this session
        </span>
      </div>
      <ErrorLine error={error || queues.error} />
      {item && (
        <div className="grid2">
          <div className="panel">
            <div className="small muted" style={{ marginBottom: 6 }}>
              {item.item.conversation_id} · {item.item.reason}
            </div>
            {item.conversation?.record && <div className="small mono muted">record {JSON.stringify(item.conversation.record)}</div>}
            {(item.conversation?.turns || []).map((t: Json, i: number) => (
              <div key={i} className={`turn ${t.role}`}>
                {t.role === "tool" ? `${t.name}(${JSON.stringify(t.args)}) → ${t.result}` : (
                  <>
                    <div className="who">{t.role}</div>
                    {t.text}
                  </>
                )}
              </div>
            ))}
          </div>
          <div className="panel">
            {checks.map((c, i) => (
              <div key={c} className={`row ${i === focus ? "focus" : ""}`} style={{ justifyContent: "space-between", padding: 6 }} onClick={() => setFocus(i)}>
                <span>{c}</span>
                <span className="choice">
                  <button className={labels[c] === 1 ? "on" : ""} onClick={() => setLabels((l) => ({ ...l, [c]: 1 }))}>yes (1)</button>
                  <button className={labels[c] === 0 ? "on" : ""} onClick={() => setLabels((l) => ({ ...l, [c]: 0 }))}>no (0)</button>
                </span>
              </div>
            ))}
            <div className="row" style={{ marginTop: 12 }}>
              <button className="primary" onClick={submit}>Submit</button>
              <button onClick={skip}>Skip</button>
            </div>
            <h2>Model scores (for reference)</h2>
            {(item.scores || []).filter((s: Json) => s.decision).map((s: Json) => (
              <div key={s.id} className="small">
                {s.check}: {s.label ?? s.value} <span className="muted">({s.evaluator.id})</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}
