package eeprocessor

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.uber.org/zap"
)

func TestRedactOffsetsQuotesAndOverlaps(t *testing.T) {
	text := `[{"role":"user","parts":[{"type":"text","content":"Soy Lucía Fernández, lucia@correo.com"}]}]`
	runes := []rune(text)
	find := func(s string) (int, int) {
		i := strings.Index(text, s)
		start := len([]rune(text[:i]))
		return start, start + len([]rune(s))
	}
	ps, pe := find("Lucía Fernández")
	es, ee := find("lucia@correo.com")
	_, ue := find("correo.com")
	out, n := redact(text, []entity{
		{Type: "PERSON", Start: ps, End: pe, Score: 0.85},
		{Type: "EMAIL_ADDRESS", Start: es, End: ee, Score: 1.0},
		{Type: "URL", Start: ue - len([]rune("correo.com")), End: ue, Score: 0.5}, // overlaps the email
	})
	if n != 2 || !strings.Contains(out, "Soy <PERSON>, <EMAIL_ADDRESS>") {
		t.Fatalf("got %d %q", n, out)
	}
	var v any
	if err := json.Unmarshal([]byte(out), &v); err != nil {
		t.Fatalf("not JSON anymore: %v", err)
	}
	// a match that spans a quote keeps the quote
	q, _ := redact(`a"b`, []entity{{Type: "X", Start: 0, End: 3}})
	if q != `<X>"<X>` {
		t.Fatalf("quote: %q", q)
	}
	if s, n := redact(string(runes), nil); n != 0 || s != text {
		t.Fatal("no entities must leave the text alone")
	}
}

func TestPresidioApply(t *testing.T) {
	var langs []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Text     string `json:"text"`
			Language string `json:"language"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		langs = append(langs, req.Language)
		var out []entity
		if i := strings.Index(req.Text, "john@x.io"); i >= 0 {
			start := len([]rune(req.Text[:i]))
			out = append(out, entity{Type: "EMAIL_ADDRESS", Start: start, End: start + 9, Score: 1})
		}
		_ = json.NewEncoder(w).Encode(out)
	}))
	defer srv.Close()

	cfg := createDefaultConfig().(*Config)
	cfg.Presidio.Endpoint = srv.URL
	td := newTraces(map[string]string{"ee.tenant_id": "acme"},
		map[string]any{"ee.lang": "es", "gen_ai.input.messages": `[{"role":"user","parts":[{"type":"text","content":"mail john@x.io"}]}]`},
		map[string]any{"gen_ai.tool.call.result": "nothing here"})
	out := run(t, cfg, context.Background(), td)
	a := attrsOf(out, 0)
	if !strings.Contains(a["gen_ai.input.messages"].(string), "mail <EMAIL_ADDRESS>") || a["ee.redacted"] != true {
		t.Fatalf("not redacted: %v", a)
	}
	if b := attrsOf(out, 1); b["gen_ai.tool.call.result"] != "nothing here" || b["ee.redacted"] != nil {
		t.Fatalf("clean text changed: %v", b)
	}
	for _, l := range langs { // the sibling span takes the language of its trace
		if l != "es" {
			t.Fatalf("language %q", l)
		}
	}

	// analyzer down: content is dropped by default, kept with on_error=keep
	cfg.Presidio.Endpoint = "http://127.0.0.1:1"
	out = run(t, cfg, context.Background(), newTraces(map[string]string{"ee.tenant_id": "acme"},
		map[string]any{"gen_ai.input.messages": "secret john@x.io"}))
	if a := attrsOf(out, 0); a["gen_ai.input.messages"] != nil || a["ee.redaction.failed"] != true ||
		a["ee.redaction.reason"] != "unreachable" || a["ee.redaction.dropped"] != int64(1) {
		t.Fatalf("drop_content: %v", a)
	}
	cfg.Presidio.OnError = "keep"
	out = run(t, cfg, context.Background(), newTraces(map[string]string{"ee.tenant_id": "acme"},
		map[string]any{"gen_ai.input.messages": "secret john@x.io"}))
	if a := attrsOf(out, 0); a["gen_ai.input.messages"] != "secret john@x.io" {
		t.Fatalf("keep: %v", a)
	}
}

func TestFailureReason(t *testing.T) {
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(300 * time.Millisecond)
	}))
	defer slow.Close()
	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) }))
	defer bad.Close()
	garbled := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("not json")) }))
	defer garbled.Close()
	for _, c := range []struct{ url, want string }{
		{slow.URL, "timeout"}, {bad.URL, "http_503"}, {garbled.URL, "bad_response"}, {"http://127.0.0.1:1", "unreachable"},
	} {
		p := newPresidio(PresidioConfig{Endpoint: c.url, Timeout: 50 * time.Millisecond}, zap.NewNop())
		_, err := p.analyze(context.Background(), "x", "en")
		if got := failureReason(err); got != c.want {
			t.Errorf("%s: reason %q, want %q (%v)", c.url, got, c.want, err)
		}
	}
	// reasons accumulate once each, the dropped count adds up
	m := pcommon.NewMap()
	markFailed(m, "timeout", true)
	markFailed(m, "timeout", true)
	markFailed(m, "http_503", true)
	if v, _ := m.Get("ee.redaction.reason"); v.Str() != "timeout,http_503" {
		t.Errorf("reasons: %q", v.Str())
	}
	if v, _ := m.Get("ee.redaction.dropped"); v.Int() != 3 {
		t.Errorf("dropped: %d", v.Int())
	}
}
