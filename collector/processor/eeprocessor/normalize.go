package eeprocessor

import (
	"encoding/json"
	"sort"
	"strconv"
	"strings"

	"go.opentelemetry.io/collector/pdata/pcommon"
)

// Normalize rewrites a span's attributes onto the pinned OpenTelemetry GenAI semantic conventions.
// Sources are tried in priority order (current gen_ai.* wins, then OpenInference, OpenLLMetry,
// Langfuse); an attribute that is already set is never overwritten. The detected source is
// recorded in ee.semconv.source. Source attributes are kept, so nothing is lost.
func Normalize(attrs pcommon.Map) {
	source := ""
	mark := func(s string) {
		if source == "" {
			source = s
		}
	}
	if _, ok := attrs.Get("gen_ai.operation.name"); ok {
		mark("genai")
	}

	// --- OpenInference -----------------------------------------------------------------------
	if kind, ok := attrs.Get("openinference.span.kind"); ok {
		mark("openinference")
		setIfAbsent(attrs, "gen_ai.operation.name", oiOperation(kind.AsString()))
		if kind.AsString() == "GUARDRAIL" || kind.AsString() == "EVALUATOR" {
			setIfAbsent(attrs, "ee.span.type", kind.AsString())
		}
	}
	copyIfAbsent(attrs, "llm.model_name", "gen_ai.request.model")
	copyIfAbsent(attrs, "llm.provider", "gen_ai.provider.name")
	copyIfAbsent(attrs, "llm.system", "gen_ai.provider.name")
	copyIntIfAbsent(attrs, "llm.token_count.prompt", "gen_ai.usage.input_tokens")
	copyIntIfAbsent(attrs, "llm.token_count.completion", "gen_ai.usage.output_tokens")
	copyIntIfAbsent(attrs, "llm.token_count.prompt_details.cache_read", "gen_ai.usage.cache_read.input_tokens")
	copyIntIfAbsent(attrs, "llm.token_count.prompt_details.cache_write", "gen_ai.usage.cache_creation.input_tokens")
	copyIntIfAbsent(attrs, "llm.token_count.completion_details.reasoning", "gen_ai.usage.reasoning.output_tokens")
	copyIfAbsent(attrs, "tool.name", "gen_ai.tool.name")
	copyIfAbsent(attrs, "tool.parameters", "gen_ai.tool.call.arguments")
	copyIfAbsent(attrs, "session.id", "gen_ai.conversation.id")
	copyIfAbsent(attrs, "user.id", "user.id")
	copyIfAbsent(attrs, "agent.name", "gen_ai.agent.name")
	if msgs := indexedMessages(attrs, "llm.input_messages.", "message."); msgs != nil {
		putJSONIfAbsent(attrs, "gen_ai.input.messages", msgs)
	}
	if msgs := indexedMessages(attrs, "llm.output_messages.", "message."); msgs != nil {
		putJSONIfAbsent(attrs, "gen_ai.output.messages", msgs)
	}
	if op, ok := attrs.Get("gen_ai.operation.name"); ok && op.AsString() == "execute_tool" {
		copyIfAbsent(attrs, "input.value", "gen_ai.tool.call.arguments")
		copyIfAbsent(attrs, "output.value", "gen_ai.tool.call.result")
	}

	// --- OpenLLMetry (Traceloop) ---------------------------------------------------------------
	if kind, ok := attrs.Get("traceloop.span.kind"); ok {
		mark("openllmetry")
		setIfAbsent(attrs, "gen_ai.operation.name", traceloopOperation(kind.AsString()))
		if kind.AsString() == "tool" {
			copyIfAbsent(attrs, "traceloop.entity.name", "gen_ai.tool.name")
			copyIfAbsent(attrs, "traceloop.entity.input", "gen_ai.tool.call.arguments")
			copyIfAbsent(attrs, "traceloop.entity.output", "gen_ai.tool.call.result")
		}
		if kind.AsString() == "agent" {
			copyIfAbsent(attrs, "traceloop.entity.name", "gen_ai.agent.name")
		}
		if kind.AsString() == "workflow" {
			copyIfAbsent(attrs, "traceloop.workflow.name", "gen_ai.workflow.name")
		}
	}
	if rt, ok := attrs.Get("llm.request.type"); ok {
		mark("openllmetry")
		setIfAbsent(attrs, "gen_ai.operation.name", map[string]string{"chat": "chat", "completion": "text_completion",
			"embedding": "embeddings"}[rt.AsString()])
	}
	copyIfAbsent(attrs, "traceloop.association.properties.session_id", "gen_ai.conversation.id")
	copyIfAbsent(attrs, "traceloop.association.properties.user_id", "user.id")
	if msgs := indexedMessages(attrs, "gen_ai.prompt.", ""); msgs != nil {
		mark("openllmetry")
		putJSONIfAbsent(attrs, "gen_ai.input.messages", msgs)
	}
	if msgs := indexedMessages(attrs, "gen_ai.completion.", ""); msgs != nil {
		putJSONIfAbsent(attrs, "gen_ai.output.messages", msgs)
	}

	// --- Langfuse SDK attributes --------------------------------------------------------------
	if t, ok := attrs.Get("langfuse.observation.type"); ok {
		mark("langfuse")
		setIfAbsent(attrs, "gen_ai.operation.name", langfuseOperation(t.AsString()))
	}
	copyIfAbsent(attrs, "langfuse.session.id", "gen_ai.conversation.id")
	copyIfAbsent(attrs, "session.id", "gen_ai.conversation.id")
	copyIfAbsent(attrs, "langfuse.user.id", "user.id")
	copyIfAbsent(attrs, "langfuse.observation.model.name", "gen_ai.request.model")
	if in, ok := attrs.Get("langfuse.observation.input"); ok {
		if msgs := langfuseMessages(in.AsString(), "user"); msgs != nil {
			putJSONIfAbsent(attrs, "gen_ai.input.messages", msgs)
		}
	}
	if out, ok := attrs.Get("langfuse.observation.output"); ok {
		if msgs := langfuseMessages(out.AsString(), "assistant"); msgs != nil {
			putJSONIfAbsent(attrs, "gen_ai.output.messages", msgs)
		}
	}
	if usage, ok := attrs.Get("langfuse.observation.usage_details"); ok {
		var u map[string]float64
		if json.Unmarshal([]byte(usage.AsString()), &u) == nil {
			for from, to := range map[string]string{"input": "gen_ai.usage.input_tokens", "output": "gen_ai.usage.output_tokens"} {
				if v, ok := u[from]; ok {
					if _, set := attrs.Get(to); !set {
						attrs.PutInt(to, int64(v))
					}
				}
			}
		}
	}

	// --- legacy gen_ai names --------------------------------------------------------------------
	copyIfAbsent(attrs, "gen_ai.system", "gen_ai.provider.name")
	copyIntIfAbsent(attrs, "gen_ai.usage.prompt_tokens", "gen_ai.usage.input_tokens")
	copyIntIfAbsent(attrs, "gen_ai.usage.completion_tokens", "gen_ai.usage.output_tokens")

	if source != "" {
		attrs.PutStr(attrSource, source)
	}
}

func oiOperation(kind string) string {
	return map[string]string{
		"LLM": "chat", "TOOL": "execute_tool", "AGENT": "invoke_agent", "CHAIN": "invoke_workflow",
		"RETRIEVER": "retrieval", "EMBEDDING": "embeddings", "RERANKER": "retrieval",
		"GUARDRAIL": "guardrail", "EVALUATOR": "evaluate",
	}[kind]
}

func traceloopOperation(kind string) string {
	return map[string]string{"workflow": "invoke_workflow", "task": "invoke_workflow", "agent": "invoke_agent",
		"tool": "execute_tool"}[kind]
}

func langfuseOperation(t string) string {
	return map[string]string{
		"generation": "chat", "tool": "execute_tool", "agent": "invoke_agent", "chain": "invoke_workflow",
		"retriever": "retrieval", "embedding": "embeddings", "guardrail": "guardrail", "evaluator": "evaluate",
	}[strings.ToLower(t)]
}

func setIfAbsent(attrs pcommon.Map, key, value string) {
	if value == "" {
		return
	}
	if _, ok := attrs.Get(key); !ok {
		attrs.PutStr(key, value)
	}
}

func copyIfAbsent(attrs pcommon.Map, from, to string) {
	if v, ok := attrs.Get(from); ok && v.AsString() != "" {
		setIfAbsent(attrs, to, v.AsString())
	}
}

func copyIntIfAbsent(attrs pcommon.Map, from, to string) {
	v, ok := attrs.Get(from)
	if !ok {
		return
	}
	if _, set := attrs.Get(to); set {
		return
	}
	switch v.Type() {
	case pcommon.ValueTypeInt:
		attrs.PutInt(to, v.Int())
	case pcommon.ValueTypeDouble:
		attrs.PutInt(to, int64(v.Double()))
	default:
		if n, err := strconv.ParseInt(v.AsString(), 10, 64); err == nil {
			attrs.PutInt(to, n)
		}
	}
}

func putJSONIfAbsent(attrs pcommon.Map, key string, value any) {
	if _, ok := attrs.Get(key); ok {
		return
	}
	if b, err := json.Marshal(value); err == nil {
		attrs.PutStr(key, string(b))
	}
}

// Message and Part follow the GenAI semconv message schema.
type Message struct {
	Role         string `json:"role"`
	Parts        []Part `json:"parts"`
	FinishReason string `json:"finish_reason,omitempty"`
}

type Part struct {
	Type      string `json:"type"`
	Content   string `json:"content,omitempty"`
	ID        string `json:"id,omitempty"`
	Name      string `json:"name,omitempty"`
	Arguments any    `json:"arguments,omitempty"`
	Response  any    `json:"response,omitempty"`
}

// indexedMessages rebuilds messages from flattened attributes such as
// prefix + "0." + inner + "role" (OpenInference: llm.input_messages.0.message.role;
// OpenLLMetry: gen_ai.prompt.0.role).
func indexedMessages(attrs pcommon.Map, prefix, inner string) []Message {
	byIndex := map[int]map[string]string{}
	for k, v := range attrs.All() {
		if !strings.HasPrefix(k, prefix) {
			continue
		}
		rest := k[len(prefix):]
		dot := strings.IndexByte(rest, '.')
		if dot < 0 {
			continue
		}
		i, err := strconv.Atoi(rest[:dot])
		if err != nil {
			continue
		}
		field := strings.TrimPrefix(rest[dot+1:], inner)
		if byIndex[i] == nil {
			byIndex[i] = map[string]string{}
		}
		byIndex[i][field] = v.AsString()
	}
	if len(byIndex) == 0 {
		return nil
	}
	idx := make([]int, 0, len(byIndex))
	for i := range byIndex {
		idx = append(idx, i)
	}
	sort.Ints(idx)
	msgs := make([]Message, 0, len(idx))
	for _, i := range idx {
		f := byIndex[i]
		m := Message{Role: f["role"], FinishReason: f["finish_reason"]}
		if f["role"] == "tool" {
			m.Parts = append(m.Parts, Part{Type: "tool_call_response", ID: f["tool_call_id"], Response: f["content"]})
		} else if c := f["content"]; c != "" {
			m.Parts = append(m.Parts, Part{Type: "text", Content: c})
		}
		for j := 0; ; j++ {
			// OpenInference: tool_calls.J.tool_call.function.name; OpenLLMetry: tool_calls.J.name
			name := firstOf(f, "tool_calls."+strconv.Itoa(j)+".tool_call.function.name", "tool_calls."+strconv.Itoa(j)+".name")
			if name == "" {
				break
			}
			args := firstOf(f, "tool_calls."+strconv.Itoa(j)+".tool_call.function.arguments", "tool_calls."+strconv.Itoa(j)+".arguments")
			id := firstOf(f, "tool_calls."+strconv.Itoa(j)+".tool_call.id", "tool_calls."+strconv.Itoa(j)+".id")
			m.Parts = append(m.Parts, Part{Type: "tool_call", ID: id, Name: name, Arguments: jsonOrString(args)})
		}
		msgs = append(msgs, m)
	}
	return msgs
}

func firstOf(f map[string]string, keys ...string) string {
	for _, k := range keys {
		if v := f[k]; v != "" {
			return v
		}
	}
	return ""
}

func jsonOrString(s string) any {
	var v any
	if json.Unmarshal([]byte(s), &v) == nil {
		return v
	}
	return s
}

// langfuseMessages accepts Langfuse's input/output shapes: a list of {role, content}, a single
// message, or plain text (attributed to defaultRole).
func langfuseMessages(raw, defaultRole string) []Message {
	var list []map[string]any
	if json.Unmarshal([]byte(raw), &list) == nil {
		var out []Message
		for _, m := range list {
			role, _ := m["role"].(string)
			if role == "" {
				continue
			}
			out = append(out, Message{Role: role, Parts: []Part{{Type: "text", Content: textOf(m["content"])}}})
		}
		return out
	}
	var one map[string]any
	if json.Unmarshal([]byte(raw), &one) == nil {
		if role, ok := one["role"].(string); ok {
			return []Message{{Role: role, Parts: []Part{{Type: "text", Content: textOf(one["content"])}}}}
		}
		return nil
	}
	if raw == "" {
		return nil
	}
	return []Message{{Role: defaultRole, Parts: []Part{{Type: "text", Content: raw}}}}
}

func textOf(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case nil:
		return ""
	default:
		b, _ := json.Marshal(t)
		return string(b)
	}
}
