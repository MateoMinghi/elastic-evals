package eeopensearchexporter

import (
	"bufio"
	"context"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/exporter/exportertest"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

func sampleTraces() ptrace.Traces {
	td := ptrace.NewTraces()
	rs := td.ResourceSpans().AppendEmpty()
	rs.Resource().Attributes().PutStr("ee.tenant_id", "acme")
	rs.Resource().Attributes().PutStr("service.name", "bank-agent")
	rs.Resource().Attributes().PutStr("deployment.environment.name", "prod")
	ss := rs.ScopeSpans().AppendEmpty()
	start := time.Date(2026, 10, 20, 10, 0, 0, 123456789, time.UTC)

	root := ss.Spans().AppendEmpty()
	root.SetTraceID([16]byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16})
	root.SetSpanID([8]byte{1, 1, 1, 1, 1, 1, 1, 1})
	root.SetName("invoke_agent bank")
	root.SetStartTimestamp(pcommon.NewTimestampFromTime(start))
	root.SetEndTimestamp(pcommon.NewTimestampFromTime(start.Add(2 * time.Second)))
	root.Attributes().PutStr("gen_ai.operation.name", "invoke_agent")
	root.Attributes().PutStr("gen_ai.agent.name", "bank")
	root.Attributes().PutStr("gen_ai.conversation.id", "conv-1")
	root.Attributes().PutStr("ee.record.status", "pending")
	root.Attributes().PutStr("http.route", "/chat")

	chat := ss.Spans().AppendEmpty()
	chat.SetTraceID(root.TraceID())
	chat.SetSpanID([8]byte{2, 2, 2, 2, 2, 2, 2, 2})
	chat.SetParentSpanID(root.SpanID())
	chat.SetName("chat gpt-x")
	chat.SetStartTimestamp(pcommon.NewTimestampFromTime(start.Add(100 * time.Millisecond)))
	chat.SetEndTimestamp(pcommon.NewTimestampFromTime(start.Add(900 * time.Millisecond)))
	chat.Status().SetCode(ptrace.StatusCodeError)
	ev := chat.Events().AppendEmpty()
	ev.SetName("exception")
	ev.Attributes().PutStr("exception.type", "TimeoutError")
	a := chat.Attributes()
	a.PutStr("gen_ai.operation.name", "chat")
	a.PutStr("gen_ai.request.model", "gpt-x-2026")
	a.PutInt("gen_ai.usage.input_tokens", 1000)
	a.PutInt("gen_ai.usage.cache_read.input_tokens", 400)
	a.PutInt("gen_ai.usage.output_tokens", 100)
	a.PutStr("gen_ai.input.messages", `[{"role":"user","parts":[{"type":"text","content":"`+strings.Repeat("hola ", 100)+`"}]}]`)
	a.PutStr("gen_ai.output.messages", `[{"role":"assistant","parts":[{"type":"tool_call","name":"transfer","arguments":{"amount":5}}]}]`)
	return td
}

func newTestExporter(t *testing.T, endpoint string) *osExporter {
	t.Helper()
	cfg := createDefaultConfig().(*Config)
	cfg.HTTP.Endpoint = endpoint
	cfg.Pools = map[string]string{"acme": "acme"}
	dir := t.TempDir()
	prices := filepath.Join(dir, "prices.json")
	_ = os.WriteFile(prices, []byte(`[
	  {"model_pattern": "^gpt-x", "start_date": "2026-01-01", "tiers": [{"above_input_tokens": 0, "input": 1.0, "output": 4.0, "cache_read": 0.1}]},
	  {"model_pattern": "^gpt-x", "start_date": "2020-01-01", "tiers": [{"above_input_tokens": 0, "input": 9, "output": 9}]}
	]`), 0o600)
	cfg.PricesFile = prices
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	e := newExporter(cfg, exportertest.NewNopSettings(Type))
	if err := e.start(context.Background(), componenttest.NewNopHost()); err != nil {
		t.Fatal(err)
	}
	return e
}

func TestBuildDocs(t *testing.T) {
	e := newTestExporter(t, "http://unused")
	items := e.buildDocs(sampleTraces(), time.Unix(0, 0))
	if len(items) != 3 { // root obs, chat stage + obs
		t.Fatalf("items: %d", len(items))
	}
	var obsChat, stage, obsRoot map[string]any
	for _, it := range items {
		if it.Pool != "acme" || it.Routing != "0102030405060708090a0b0c0d0e0f10" {
			t.Fatalf("pool/routing: %+v", it)
		}
		switch {
		case it.Kind == "stage":
			stage = it.Doc
		case it.Doc["span_id"] == "0202020202020202":
			obsChat = it.Doc
		default:
			obsRoot = it.Doc
		}
	}
	if obsRoot["type"] != "AGENT" || obsRoot["session_id"] != "conv-1" || obsRoot["agent_name"] != "bank" ||
		obsRoot["environment"] != "prod" || obsRoot["start_time"] != "2026-10-20T10:00:00.123456789Z" {
		t.Fatalf("root: %v", obsRoot)
	}
	if obsRoot["record_refs"].(map[string]any)["status"] != "pending" || obsRoot["attributes"].(map[string]any)["http.route"] != "/chat" {
		t.Fatalf("root attrs: %v", obsRoot)
	}
	if obsChat["type"] != "GENERATION" || obsChat["status"] != "ERROR" || obsChat["error_type"] != "TimeoutError" ||
		obsChat["parent_span_id"] != "0101010101010101" || obsChat["has_payload"] != true || obsChat["duration_ms"] != 800.0 {
		t.Fatalf("chat: %v", obsChat)
	}
	// exclusive buckets with the newest matching price: (600*1 + 400*0.1 + 100*4) / 1e6
	if c, _ := obsChat["cost_usd"].(float64); math.Abs(c-0.00104) > 1e-12 {
		t.Fatalf("cost: %v", c)
	}
	if p := []rune(obsChat["input_preview"].(string)); len(p) != 201 {
		t.Fatalf("preview length %d", len(p))
	}
	if attrs, _ := obsChat["attributes"].(map[string]any); attrs["gen_ai.input.messages"] != nil {
		t.Fatal("content must not be copied into obs attributes")
	}
	if !strings.Contains(stage["output_text"].(string), "call transfer") || stage["raw"].(map[string]any)["input_messages"] == nil {
		t.Fatalf("stage: %v", stage)
	}
}

func TestBulkHandling(t *testing.T) {
	var lines []string
	status := map[string]int{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sc := bufio.NewScanner(r.Body)
		sc.Buffer(make([]byte, 1<<20), 1<<20)
		var items []map[string]any
		i := 0
		for sc.Scan() {
			lines = append(lines, sc.Text())
			if i%2 == 0 {
				var op map[string]map[string]any
				_ = json.Unmarshal(sc.Bytes(), &op)
				id := op["create"]["_id"].(string)
				st := status[id]
				if st == 0 {
					st = 201
				}
				items = append(items, map[string]any{"create": map[string]any{"status": st, "error": map[string]any{"type": "x"}}})
			}
			i++
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"errors": true, "items": items})
	}))
	defer srv.Close()
	e := newTestExporter(t, srv.URL)
	ctx := context.Background()

	if err := e.pushTraces(ctx, sampleTraces()); err != nil {
		t.Fatalf("clean batch: %v", err)
	}
	if len(lines) != 6 || !strings.Contains(lines[0], `"_index":"ee-`) {
		t.Fatalf("ndjson: %v", lines[:2])
	}
	id := "0102030405060708090a0b0c0d0e0f10-0101010101010101"
	status[id] = 409 // already written: fine
	if err := e.pushTraces(ctx, sampleTraces()); err != nil {
		t.Fatalf("409 must be ignored: %v", err)
	}
	status[id] = 400 // mapping error: dropped, not retried
	if err := e.pushTraces(ctx, sampleTraces()); err != nil {
		t.Fatalf("400 must be dropped: %v", err)
	}
	status[id] = 429 // throttled: retry the batch
	if err := e.pushTraces(ctx, sampleTraces()); err == nil {
		t.Fatal("429 must be retried")
	}
}
