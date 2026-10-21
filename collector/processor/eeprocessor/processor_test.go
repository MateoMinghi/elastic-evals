package eeprocessor

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"go.opentelemetry.io/collector/client"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor/processortest"
)

func newTraces(resAttrs map[string]string, spans ...map[string]any) ptrace.Traces {
	td := ptrace.NewTraces()
	rs := td.ResourceSpans().AppendEmpty()
	for k, v := range resAttrs {
		rs.Resource().Attributes().PutStr(k, v)
	}
	ss := rs.ScopeSpans().AppendEmpty()
	for _, attrs := range spans {
		sp := ss.Spans().AppendEmpty()
		sp.SetName("span")
		_ = sp.Attributes().FromRaw(attrs)
	}
	return td
}

func run(t *testing.T, cfg *Config, ctx context.Context, td ptrace.Traces) ptrace.Traces {
	t.Helper()
	sink := new(consumertest.TracesSink)
	p, err := NewFactory().CreateTraces(ctx, processortest.NewNopSettings(Type), cfg, sink)
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Start(ctx, componenttest.NewNopHost()); err != nil {
		t.Fatal(err)
	}
	if err := p.ConsumeTraces(ctx, td); err != nil {
		t.Fatal(err)
	}
	all := sink.AllTraces()
	if len(all) == 0 {
		return ptrace.NewTraces()
	}
	return all[0]
}

func attrsOf(td ptrace.Traces, i int) map[string]any {
	return td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(i).Attributes().AsRaw()
}

func TestTenantFromResourceMetadataDefault(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	cfg.Tenant.FromMetadata = "x-ee-tenant"
	cfg.Tenant.Default = "fallback"

	out := run(t, cfg, context.Background(), newTraces(map[string]string{"ee.tenant_id": "acme"}, map[string]any{}))
	if v, _ := out.ResourceSpans().At(0).Resource().Attributes().Get("ee.tenant_id"); v.Str() != "acme" {
		t.Fatalf("resource tenant: got %q", v.Str())
	}
	ctx := client.NewContext(context.Background(), client.Info{Metadata: client.NewMetadata(map[string][]string{"x-ee-tenant": {"beta"}})})
	out = run(t, cfg, ctx, newTraces(nil, map[string]any{}))
	if v, _ := out.ResourceSpans().At(0).Resource().Attributes().Get("ee.tenant_id"); v.Str() != "beta" {
		t.Fatalf("metadata tenant: got %q", v.Str())
	}
	out = run(t, cfg, context.Background(), newTraces(nil, map[string]any{}))
	if v, _ := out.ResourceSpans().At(0).Resource().Attributes().Get("ee.tenant_id"); v.Str() != "fallback" {
		t.Fatalf("default tenant: got %q", v.Str())
	}
}

func TestRequireTenantDrops(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	cfg.Tenant.Require = true
	out := run(t, cfg, context.Background(), newTraces(nil, map[string]any{"a": 1}))
	if out.SpanCount() != 0 {
		t.Fatalf("expected spans without tenant to be dropped, got %d", out.SpanCount())
	}
}

func TestInternalEnvironmentAndDropContent(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	cfg.DropContent = true
	out := run(t, cfg, context.Background(), newTraces(
		map[string]string{"ee.tenant_id": "acme", "deployment.environment.name": "ee-internal"},
		map[string]any{"gen_ai.operation.name": "chat", "gen_ai.input.messages": "[]"}))
	a := attrsOf(out, 0)
	if a["ee.internal"] != true {
		t.Fatal("expected ee.internal")
	}
	if _, ok := a["gen_ai.input.messages"]; ok {
		t.Fatal("content should be dropped")
	}
}

func TestNormalizeOpenInference(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	out := run(t, cfg, context.Background(), newTraces(map[string]string{"ee.tenant_id": "acme"}, map[string]any{
		"openinference.span.kind":              "LLM",
		"llm.model_name":                       "gpt-x",
		"llm.provider":                         "openai",
		"llm.token_count.prompt":               int64(120),
		"llm.token_count.completion":           int64(30),
		"session.id":                           "sess-1",
		"llm.input_messages.0.message.role":    "system",
		"llm.input_messages.0.message.content": "be nice",
		"llm.input_messages.1.message.role":    "user",
		"llm.input_messages.1.message.content": "hola",
		"llm.output_messages.0.message.role":   "assistant",
		"llm.output_messages.0.message.tool_calls.0.tool_call.function.name":      "get_balance",
		"llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments": `{"account":"1"}`,
	}, map[string]any{
		"openinference.span.kind": "TOOL", "tool.name": "get_balance", "input.value": `{"account":"1"}`, "output.value": "100",
	}))
	a := attrsOf(out, 0)
	want := map[string]any{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-x", "gen_ai.provider.name": "openai",
		"gen_ai.usage.input_tokens": int64(120), "gen_ai.usage.output_tokens": int64(30), "gen_ai.conversation.id": "sess-1",
		"ee.semconv.source": "openinference"}
	for k, v := range want {
		if a[k] != v {
			t.Errorf("%s: got %v want %v", k, a[k], v)
		}
	}
	var in []Message
	if err := json.Unmarshal([]byte(a["gen_ai.input.messages"].(string)), &in); err != nil || len(in) != 2 || in[1].Parts[0].Content != "hola" {
		t.Fatalf("input messages: %v %v", in, err)
	}
	var outMsgs []Message
	_ = json.Unmarshal([]byte(a["gen_ai.output.messages"].(string)), &outMsgs)
	if len(outMsgs) != 1 || outMsgs[0].Parts[0].Type != "tool_call" || outMsgs[0].Parts[0].Name != "get_balance" {
		t.Fatalf("output messages: %+v", outMsgs)
	}
	tool := attrsOf(out, 1)
	if tool["gen_ai.operation.name"] != "execute_tool" || tool["gen_ai.tool.name"] != "get_balance" || tool["gen_ai.tool.call.result"] != "100" {
		t.Fatalf("tool span: %v", tool)
	}
}

func TestNormalizeOpenLLMetryAndLangfuse(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	out := run(t, cfg, context.Background(), newTraces(map[string]string{"ee.tenant_id": "acme"}, map[string]any{
		"llm.request.type":               "chat",
		"gen_ai.system":                  "Anthropic",
		"gen_ai.usage.prompt_tokens":     int64(10),
		"gen_ai.usage.completion_tokens": int64(5),
		"gen_ai.prompt.0.role":           "user",
		"gen_ai.prompt.0.content":        "hi",
		"gen_ai.completion.0.role":       "assistant",
		"gen_ai.completion.0.content":    "hello",
	}, map[string]any{
		"langfuse.observation.type":   "generation",
		"langfuse.session.id":         "s9",
		"langfuse.observation.input":  `[{"role":"user","content":"q"}]`,
		"langfuse.observation.output": "plain answer",
	}, map[string]any{
		"gen_ai.operation.name": "chat", "gen_ai.input.messages": `[{"role":"user","parts":[]}]`, "openinference.span.kind": "LLM",
	}))
	a := attrsOf(out, 0)
	if a["gen_ai.operation.name"] != "chat" || a["gen_ai.provider.name"] != "Anthropic" || a["gen_ai.usage.input_tokens"] != int64(10) ||
		a["ee.semconv.source"] != "openllmetry" {
		t.Fatalf("openllmetry: %v", a)
	}
	b := attrsOf(out, 1)
	if b["gen_ai.conversation.id"] != "s9" || b["ee.semconv.source"] != "langfuse" {
		t.Fatalf("langfuse: %v", b)
	}
	var o []Message
	_ = json.Unmarshal([]byte(b["gen_ai.output.messages"].(string)), &o)
	if len(o) != 1 || o[0].Role != "assistant" || o[0].Parts[0].Content != "plain answer" {
		t.Fatalf("langfuse output: %+v", o)
	}
	c := attrsOf(out, 2)
	if c["ee.semconv.source"] != "genai" || c["gen_ai.input.messages"] != `[{"role":"user","parts":[]}]` {
		t.Fatalf("native gen_ai must win: %v", c)
	}
}

func TestNormalizeOpenInferenceAgentTurn(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	out := run(t, cfg, context.Background(), newTraces(map[string]string{"ee.tenant_id": "acme"}, map[string]any{
		"openinference.span.kind": "AGENT", "session.id": "s-1",
		"input.value": "Book me a flight to Lisbon", "output.value": "Booked: AF123, 120 EUR",
	}, map[string]any{
		"openinference.span.kind": "AGENT", "input.value": `{"messages": []}`, "output.value": "ok", "output.mime_type": "text/plain",
	}, map[string]any{
		"openinference.span.kind":            "LLM",
		"llm.output_messages.0.message.role": "assistant", // a tool-call-only message: no text
	}))
	a := attrsOf(out, 0)
	var in, outMsgs []Message
	_ = json.Unmarshal([]byte(a["gen_ai.input.messages"].(string)), &in)
	_ = json.Unmarshal([]byte(a["gen_ai.output.messages"].(string)), &outMsgs)
	if len(in) != 1 || in[0].Role != "user" || in[0].Parts[0].Content != "Book me a flight to Lisbon" ||
		len(outMsgs) != 1 || outMsgs[0].Role != "assistant" {
		t.Fatalf("agent turn: %v %v", in, outMsgs)
	}
	b := attrsOf(out, 1)
	if _, ok := b["gen_ai.input.messages"]; ok {
		t.Fatalf("JSON input.value must not become a user message: %v", b["gen_ai.input.messages"])
	}
	if b["gen_ai.output.messages"] == nil {
		t.Fatalf("text/plain output.value should become the assistant message")
	}
	if got := attrsOf(out, 2)["gen_ai.output.messages"].(string); !strings.Contains(got, `"parts":[]`) {
		t.Fatalf("parts must be [] not null: %s", got)
	}
}
