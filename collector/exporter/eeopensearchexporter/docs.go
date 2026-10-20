package eeopensearchexporter

import (
	"encoding/json"
	"strings"
	"time"
	"unicode/utf8"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// item is one bulk operation: index kind, pool, _id, routing and the document.
type item struct {
	Kind    string
	Pool    string
	ID      string
	Routing string
	Doc     map[string]any
}

// Attributes mapped to typed columns or to the staged content; everything else goes to `attributes`.
var mapped = map[string]bool{
	"ee.tenant_id": true, "ee.project_id": true, "ee.internal": true, "ee.eval_flag": true, "ee.sentry.event_id": true,
	"ee.experiment.run_id": true, "ee.experiment.item_id": true, "ee.semconv.source": true, "ee.tags": true, "ee.span.type": true,
	"ee.redacted": true, "ee.redaction.failed": true,
	"gen_ai.conversation.id": true, "gen_ai.operation.name": true, "gen_ai.agent.name": true, "gen_ai.agent.id": true,
	"gen_ai.workflow.name": true, "gen_ai.provider.name": true, "gen_ai.request.model": true, "gen_ai.response.model": true,
	"gen_ai.tool.name": true, "gen_ai.tool.call.id": true, "gen_ai.usage.input_tokens": true, "gen_ai.usage.output_tokens": true,
	"gen_ai.usage.cache_read.input_tokens": true, "gen_ai.usage.cache_creation.input_tokens": true,
	"gen_ai.usage.reasoning.output_tokens": true, "user.id": true, "enduser.id": true, "error.type": true,
	"gen_ai.input.messages": true, "gen_ai.output.messages": true, "gen_ai.system_instructions": true,
	"gen_ai.tool.call.arguments": true, "gen_ai.tool.call.result": true,
}

// Source attributes that duplicate normalised content; not copied into obs.attributes.
var contentPrefixes = []string{"llm.input_messages.", "llm.output_messages.", "gen_ai.prompt.", "gen_ai.completion.",
	"input.value", "output.value", "langfuse.observation.input", "langfuse.observation.output", "traceloop.entity.input",
	"traceloop.entity.output", "tool.parameters"}

func (e *osExporter) buildDocs(td ptrace.Traces, now time.Time) []item {
	var out []item
	ingested := now.UTC().Format(time.RFC3339Nano)
	for _, rs := range td.ResourceSpans().All() {
		res := rs.Resource().Attributes()
		tenant := str(res, "ee.tenant_id")
		pool := "shared"
		if p, ok := e.cfg.Pools[tenant]; ok {
			pool = p
		}
		resource := res.AsRaw()
		service := str(res, "service.name")
		env := firstStr(res, "deployment.environment.name", "deployment.environment")
		release := str(res, "service.version")
		for _, ss := range rs.ScopeSpans().All() {
			for _, sp := range ss.Spans().All() {
				a := sp.Attributes()
				traceID, spanID := sp.TraceID().String(), sp.SpanID().String()
				start, end := sp.StartTimestamp().AsTime(), sp.EndTimestamp().AsTime()
				op := str(a, "gen_ai.operation.name")
				doc := map[string]any{
					"tenant_id": tenant, "trace_id": traceID, "span_id": spanID, "name": sp.Name(),
					"span_kind": strings.TrimPrefix(sp.Kind().String(), "SPAN_KIND_"), "type": spanType(op, str(a, "ee.span.type")),
					"start_time": start.UTC().Format(time.RFC3339Nano), "end_time": end.UTC().Format(time.RFC3339Nano),
					"duration_ms": float64(end.Sub(start).Microseconds()) / 1000, "status": statusOf(sp),
					"service_name": service, "environment": env, "release": release, "resource": resource,
					"ingested_at": ingested,
				}
				if !sp.ParentSpanID().IsEmpty() {
					doc["parent_span_id"] = sp.ParentSpanID().String()
				}
				if msg := sp.Status().Message(); msg != "" {
					doc["status_message"] = msg
				}
				setStr(doc, "project_id", firstStr(a, "ee.project_id"), firstStr(res, "ee.project_id"))
				setStr(doc, "session_id", str(a, "gen_ai.conversation.id"), str(res, "gen_ai.conversation.id"))
				setStr(doc, "operation", op)
				setStr(doc, "error_type", str(a, "error.type"), exceptionType(sp))
				setStr(doc, "user_id", str(a, "user.id"), str(a, "enduser.id"), str(res, "user.id"))
				setStr(doc, "agent_name", str(a, "gen_ai.agent.name"), str(res, "gen_ai.agent.name"))
				setStr(doc, "agent_id", str(a, "gen_ai.agent.id"))
				setStr(doc, "workflow_name", str(a, "gen_ai.workflow.name"))
				setStr(doc, "provider", str(a, "gen_ai.provider.name"))
				setStr(doc, "request_model", str(a, "gen_ai.request.model"))
				setStr(doc, "response_model", str(a, "gen_ai.response.model"))
				setStr(doc, "tool_name", str(a, "gen_ai.tool.name"))
				setStr(doc, "tool_call_id", str(a, "gen_ai.tool.call.id"))
				setStr(doc, "sentry_event_id", str(a, "ee.sentry.event_id"))
				setStr(doc, "experiment_run_id", str(a, "ee.experiment.run_id"), str(res, "ee.experiment.run_id"))
				setStr(doc, "experiment_item_id", str(a, "ee.experiment.item_id"))
				setStr(doc, "semconv_source", str(a, "ee.semconv.source"))
				if tags := strList(a, "ee.tags"); len(tags) > 0 {
					doc["tags"] = tags
				} else if tags := strList(res, "ee.tags"); len(tags) > 0 {
					doc["tags"] = tags
				}
				if v, ok := a.Get("ee.internal"); ok && v.Bool() {
					doc["internal"] = true
				}
				if v, ok := a.Get("ee.redaction.failed"); ok && v.Bool() {
					doc["redaction_failed"] = true
				}
				if v, ok := a.Get("ee.eval_flag"); ok && v.Bool() {
					doc["eval_flag"] = true
				}

				usage := map[string]int64{}
				for k, col := range map[string]string{
					"gen_ai.usage.input_tokens": "input_tokens", "gen_ai.usage.output_tokens": "output_tokens",
					"gen_ai.usage.cache_read.input_tokens": "cache_read_tokens", "gen_ai.usage.cache_creation.input_tokens": "cache_write_tokens",
					"gen_ai.usage.reasoning.output_tokens": "reasoning_tokens",
				} {
					if v, ok := a.Get(k); ok {
						usage[col] = intOf(v)
					}
				}
				if len(usage) > 0 {
					usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
					doc["usage"] = usage
					model := firstStr(a, "gen_ai.response.model", "gen_ai.request.model")
					if cost, priceID, ok := e.book().cost(model, usage, start); ok {
						doc["cost_usd"] = cost
						doc["price_id"] = priceID
					}
				}

				records, attrs := map[string]any{}, map[string]any{}
				for k, v := range a.All() {
					switch {
					case strings.HasPrefix(k, "ee.record."):
						records[strings.TrimPrefix(k, "ee.record.")] = v.AsRaw()
					case mapped[k] || hasAnyPrefix(k, contentPrefixes):
					default:
						attrs[k] = v.AsRaw()
					}
				}
				if len(records) > 0 {
					doc["record_refs"] = records
				}
				if len(attrs) > 0 {
					doc["attributes"] = attrs
				}

				content := contentOf(a)
				if len(content) > 0 {
					in, outText, sys := renderContent(content)
					setStr(doc, "input_preview", truncate(in, e.cfg.PreviewChars))
					setStr(doc, "output_preview", truncate(outText, e.cfg.PreviewChars))
					if e.cfg.Content == "stage" {
						doc["has_payload"] = true
						stage := map[string]any{
							"tenant_id": tenant, "trace_id": traceID, "span_id": spanID, "type": doc["type"],
							"start_time": doc["start_time"], "raw": content, "ingested_at": ingested,
						}
						setStr(stage, "session_id", doc["session_id"])
						if v, ok := a.Get("ee.redacted"); ok && v.Bool() {
							stage["redacted"] = true
						}
						setStr(stage, "input_text", in)
						setStr(stage, "output_text", outText)
						setStr(stage, "system_text", sys)
						out = append(out, item{Kind: "stage", Pool: pool, ID: traceID + "-" + spanID, Routing: traceID, Doc: stage})
					}
				}
				out = append(out, item{Kind: "obs", Pool: pool, ID: traceID + "-" + spanID, Routing: traceID, Doc: doc})
			}
		}
	}
	return out
}

// spanType is the Langfuse-style observation type used by list views.
func spanType(op, explicit string) string {
	if explicit != "" {
		return explicit
	}
	switch op {
	case "chat", "text_completion", "generate_content":
		return "GENERATION"
	case "execute_tool":
		return "TOOL"
	case "invoke_agent", "create_agent":
		return "AGENT"
	case "embeddings":
		return "EMBEDDING"
	case "retrieval":
		return "RETRIEVER"
	case "invoke_workflow":
		return "CHAIN"
	case "guardrail":
		return "GUARDRAIL"
	case "evaluate":
		return "EVALUATOR"
	}
	return "SPAN"
}

func statusOf(sp ptrace.Span) string {
	switch sp.Status().Code() {
	case ptrace.StatusCodeError:
		return "ERROR"
	case ptrace.StatusCodeOk:
		return "OK"
	}
	return "UNSET"
}

func exceptionType(sp ptrace.Span) string {
	for _, ev := range sp.Events().All() {
		if ev.Name() == "exception" {
			if v, ok := ev.Attributes().Get("exception.type"); ok {
				return v.AsString()
			}
		}
	}
	return ""
}

// contentOf returns the span's content attributes verbatim (strings stay strings): `raw` is
// stored unindexed and read back by the evaluation worker, so re-encoding would change what the
// evaluators see (key order, number formatting).
func contentOf(a pcommon.Map) map[string]any {
	out := map[string]any{}
	for _, k := range []string{"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
		"gen_ai.tool.call.arguments", "gen_ai.tool.call.result"} {
		if v, ok := a.Get(k); ok {
			out[strings.ReplaceAll(strings.TrimPrefix(k, "gen_ai."), ".", "_")] = v.AsRaw()
		}
	}
	return out
}

func parsed(v any) any {
	if s, ok := v.(string); ok {
		var p any
		if json.Unmarshal([]byte(s), &p) == nil {
			return p
		}
	}
	return v
}

// renderContent flattens content to searchable text: input, output and system instructions.
func renderContent(c map[string]any) (in, out, sys string) {
	in = messagesText(parsed(c["input_messages"]))
	out = messagesText(parsed(c["output_messages"]))
	sys = messagesText(parsed(c["system_instructions"]))
	if args, ok := c["tool_call_arguments"]; ok && in == "" {
		in = asText(args)
	}
	if res, ok := c["tool_call_result"]; ok && out == "" {
		out = asText(res)
	}
	return
}

func messagesText(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case []any:
		var b strings.Builder
		for _, m := range t {
			mm, ok := m.(map[string]any)
			if !ok {
				b.WriteString(asText(m))
				continue
			}
			if parts, ok := mm["parts"].([]any); ok {
				for _, p := range parts {
					pm, _ := p.(map[string]any)
					switch pm["type"] {
					case "text":
						appendLine(&b, mm["role"], asText(pm["content"]))
					case "tool_call":
						appendLine(&b, mm["role"], "call "+asText(pm["name"])+" "+asText(pm["arguments"]))
					case "tool_call_response":
						appendLine(&b, mm["role"], asText(pm["response"]))
					}
				}
			} else if c, ok := mm["content"]; ok { // system instructions / plain parts
				appendLine(&b, mm["role"], asText(c))
			}
		}
		return strings.TrimSpace(b.String())
	}
	return asText(v)
}

func appendLine(b *strings.Builder, role any, text string) {
	if text == "" {
		return
	}
	if r, ok := role.(string); ok && r != "" {
		b.WriteString(r + ": ")
	}
	b.WriteString(text)
	b.WriteString("\n")
}

func asText(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	}
	b, _ := json.Marshal(v)
	return string(b)
}

func truncate(s string, n int) string {
	if n <= 0 || utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return string(r[:n]) + "…"
}

func str(m pcommon.Map, k string) string {
	if v, ok := m.Get(k); ok {
		return v.AsString()
	}
	return ""
}

func firstStr(m pcommon.Map, keys ...string) string {
	for _, k := range keys {
		if s := str(m, k); s != "" {
			return s
		}
	}
	return ""
}

func strList(m pcommon.Map, k string) []string {
	v, ok := m.Get(k)
	if !ok {
		return nil
	}
	if v.Type() == pcommon.ValueTypeSlice {
		out := make([]string, 0, v.Slice().Len())
		for _, x := range v.Slice().All() {
			out = append(out, x.AsString())
		}
		return out
	}
	if s := v.AsString(); s != "" {
		return strings.Split(s, ",")
	}
	return nil
}

func intOf(v pcommon.Value) int64 {
	switch v.Type() {
	case pcommon.ValueTypeInt:
		return v.Int()
	case pcommon.ValueTypeDouble:
		return int64(v.Double())
	}
	var n int64
	_ = json.Unmarshal([]byte(v.AsString()), &n)
	return n
}

// setStr sets doc[k] to the first non-empty candidate.
func setStr(doc map[string]any, k string, candidates ...any) {
	for _, c := range candidates {
		if s, ok := c.(string); ok && s != "" {
			doc[k] = s
			return
		}
	}
}

func hasAnyPrefix(s string, prefixes []string) bool {
	for _, p := range prefixes {
		if strings.HasPrefix(s, p) {
			return true
		}
	}
	return false
}
