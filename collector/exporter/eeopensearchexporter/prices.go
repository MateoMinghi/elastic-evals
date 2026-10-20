package eeopensearchexporter

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
	"time"
)

// Price is one model_prices row: USD per million tokens, matched by regex on the model name,
// effective from StartDate, with tiers by input size (the highest tier whose threshold the
// input reaches applies to the whole call).
type Price struct {
	ID           string `json:"id"`
	ModelPattern string `json:"model_pattern"`
	StartDate    string `json:"start_date"`
	Priority     int    `json:"priority"`
	Tiers        []Tier `json:"tiers"`

	re    *regexp.Regexp
	start time.Time
}

type Tier struct {
	AboveInputTokens int64   `json:"above_input_tokens"`
	Input            float64 `json:"input"`
	Output           float64 `json:"output"`
	CacheRead        float64 `json:"cache_read"`
	CacheWrite       float64 `json:"cache_write"`
}

type priceBook []*Price

func loadPrices(path string) (priceBook, error) {
	if path == "" {
		return nil, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var book priceBook
	if err := json.Unmarshal(raw, &book); err != nil {
		return nil, fmt.Errorf("prices file %s: %w", path, err)
	}
	for i, p := range book {
		if p.re, err = regexp.Compile(p.ModelPattern); err != nil {
			return nil, fmt.Errorf("price %d: %w", i, err)
		}
		if p.StartDate != "" {
			if p.start, err = time.Parse("2006-01-02", p.StartDate); err != nil {
				return nil, fmt.Errorf("price %d: %w", i, err)
			}
		}
		if p.ID == "" {
			p.ID = p.ModelPattern + "@" + p.StartDate
		}
		sort.Slice(p.Tiers, func(a, b int) bool { return p.Tiers[a].AboveInputTokens < p.Tiers[b].AboveInputTokens })
	}
	// highest priority first, then the most recent start date
	sort.SliceStable(book, func(a, b int) bool {
		if book[a].Priority != book[b].Priority {
			return book[a].Priority > book[b].Priority
		}
		return book[a].start.After(book[b].start)
	})
	return book, nil
}

// cost uses exclusive token buckets: cached reads and cache writes are billed at their own rates
// and taken out of the plain input count.
func (b priceBook) cost(model string, u map[string]int64, at time.Time) (float64, string, bool) {
	if model == "" {
		return 0, "", false
	}
	for _, p := range b {
		if !p.re.MatchString(model) || at.Before(p.start) || len(p.Tiers) == 0 {
			continue
		}
		t := p.Tiers[0]
		for _, tier := range p.Tiers {
			if u["input_tokens"] >= tier.AboveInputTokens {
				t = tier
			}
		}
		plain := u["input_tokens"] - u["cache_read_tokens"] - u["cache_write_tokens"]
		if plain < 0 {
			plain = 0
		}
		usd := (float64(plain)*t.Input + float64(u["cache_read_tokens"])*t.CacheRead +
			float64(u["cache_write_tokens"])*t.CacheWrite + float64(u["output_tokens"])*t.Output) / 1e6
		return usd, p.ID, true
	}
	return 0, "", false
}
