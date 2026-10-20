package eeprocessor

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.uber.org/zap"
)

// PresidioConfig enables redaction of conversation content through a Presidio analyzer
// (Microsoft's presidio-analyzer image or `python -m elastic_evals presidio`). Detected entities are
// replaced in the attribute text itself, so the stored content keeps its shape (JSON stays JSON).
type PresidioConfig struct {
	// Analyzer base URL, e.g. http://presidio:3000. Empty: disabled.
	Endpoint string `mapstructure:"endpoint"`
	// Language when the span or resource has no LanguageAttribute.
	Language          string        `mapstructure:"language"`
	LanguageAttribute string        `mapstructure:"language_attribute"`
	Entities          []string      `mapstructure:"entities"`
	ScoreThreshold    float64       `mapstructure:"score_threshold"`
	Attributes        []string      `mapstructure:"attributes"`
	Timeout           time.Duration `mapstructure:"timeout"`
	Concurrency       int           `mapstructure:"concurrency"`
	// "drop_content" (default): if the analyzer fails, remove the content attributes; "keep": forward them unredacted.
	OnError string `mapstructure:"on_error"`
}

func defaultPresidio() PresidioConfig {
	return PresidioConfig{
		Language: "en", LanguageAttribute: "ee.lang", ScoreThreshold: 0.5, Timeout: 5 * time.Second, Concurrency: 8,
		OnError:    "drop_content",
		Attributes: append([]string(nil), ContentAttributes...),
		Entities: []string{"PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE", "IP_ADDRESS",
			"US_SSN", "US_PASSPORT", "US_BANK_NUMBER", "CRYPTO", "MEDICAL_LICENSE"},
	}
}

type entity struct {
	Type  string  `json:"entity_type"`
	Start int     `json:"start"`
	End   int     `json:"end"`
	Score float64 `json:"score"`
}

type presidio struct {
	cfg    PresidioConfig
	client *http.Client
	logger *zap.Logger
}

func newPresidio(cfg PresidioConfig, logger *zap.Logger) *presidio {
	if cfg.Concurrency <= 0 {
		cfg.Concurrency = 8
	}
	return &presidio{cfg: cfg, client: &http.Client{Timeout: cfg.Timeout}, logger: logger}
}

func (p *presidio) analyze(ctx context.Context, text, lang string) ([]entity, error) {
	body, _ := json.Marshal(map[string]any{"text": text, "language": lang, "entities": p.cfg.Entities,
		"score_threshold": p.cfg.ScoreThreshold})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(p.cfg.Endpoint, "/")+"/analyze", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := p.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("presidio: %s", resp.Status)
	}
	var out []entity
	return out, json.NewDecoder(resp.Body).Decode(&out)
}

// redact replaces entities (offsets in Unicode code points, as Presidio reports them). Overlapping
// entities merge and take the type of the highest score. Quote and backslash characters inside a
// match are kept, so content that is JSON text stays valid JSON.
func redact(text string, ents []entity) (string, int) {
	if len(ents) == 0 {
		return text, 0
	}
	sort.Slice(ents, func(i, j int) bool { return ents[i].Start < ents[j].Start })
	var merged []entity
	for _, e := range ents {
		if n := len(merged); n > 0 && e.Start < merged[n-1].End {
			last := &merged[n-1]
			if e.End > last.End {
				last.End = e.End
			}
			if e.Score > last.Score {
				last.Type, last.Score = e.Type, e.Score
			}
			continue
		}
		merged = append(merged, e)
	}
	runes := []rune(text)
	var b strings.Builder
	pos := 0
	for _, e := range merged {
		if e.Start < pos || e.End > len(runes) || e.Start >= e.End {
			continue
		}
		b.WriteString(string(runes[pos:e.Start]))
		token := "<" + e.Type + ">"
		inRun := false
		for _, r := range runes[e.Start:e.End] {
			if r == '"' || r == '\\' {
				b.WriteRune(r)
				inRun = false
				continue
			}
			if !inRun {
				b.WriteString(token)
				inRun = true
			}
		}
		pos = e.End
	}
	b.WriteString(string(runes[pos:]))
	return b.String(), len(merged)
}

type target struct {
	attrs pcommon.Map
	key   string
	text  string
	lang  string
}

// apply redacts every configured content attribute of the batch, calling the analyzer concurrently.
func (p *presidio) apply(ctx context.Context, td ptrace.Traces) {
	var targets []target
	traceLang := map[pcommon.TraceID]string{} // usually only the root span names the language
	for _, rs := range td.ResourceSpans().All() {
		for _, ss := range rs.ScopeSpans().All() {
			for _, sp := range ss.Spans().All() {
				if l := str(sp.Attributes(), p.cfg.LanguageAttribute); l != "" {
					traceLang[sp.TraceID()] = l
				}
			}
		}
	}
	for _, rs := range td.ResourceSpans().All() {
		resLang := str(rs.Resource().Attributes(), p.cfg.LanguageAttribute)
		for _, ss := range rs.ScopeSpans().All() {
			for _, sp := range ss.Spans().All() {
				a := sp.Attributes()
				lang := str(a, p.cfg.LanguageAttribute)
				if lang == "" {
					lang = traceLang[sp.TraceID()]
				}
				if lang == "" {
					lang = resLang
				}
				if lang == "" {
					lang = p.cfg.Language
				}
				for _, k := range p.cfg.Attributes {
					if v, ok := a.Get(k); ok && v.Type() == pcommon.ValueTypeStr && v.Str() != "" {
						targets = append(targets, target{attrs: a, key: k, text: v.Str(), lang: lang})
					}
				}
			}
		}
	}
	type result struct {
		text string
		n    int
		err  error
	}
	results := make([]result, len(targets))
	sem := make(chan struct{}, p.cfg.Concurrency)
	var wg sync.WaitGroup
	for i, t := range targets {
		wg.Add(1)
		sem <- struct{}{}
		go func(i int, t target) {
			defer wg.Done()
			defer func() { <-sem }()
			ents, err := p.analyze(ctx, t.text, t.lang)
			if err != nil {
				results[i] = result{err: err}
				return
			}
			text, n := redact(t.text, ents)
			results[i] = result{text: text, n: n}
		}(i, t)
	}
	wg.Wait()
	failed := 0
	for i, t := range targets {
		r := results[i]
		switch {
		case r.err != nil:
			failed++
			if p.cfg.OnError != "keep" {
				t.attrs.Remove(t.key)
				t.attrs.PutBool("ee.redaction.failed", true)
			}
		case r.n > 0:
			t.attrs.PutStr(t.key, r.text)
			t.attrs.PutBool("ee.redacted", true)
		}
	}
	if failed > 0 {
		p.logger.Warn("presidio redaction failed", zap.Int("attributes", failed), zap.String("on_error", p.cfg.OnError))
	}
}

func str(m pcommon.Map, k string) string {
	if k == "" {
		return ""
	}
	if v, ok := m.Get(k); ok {
		return v.AsString()
	}
	return ""
}
