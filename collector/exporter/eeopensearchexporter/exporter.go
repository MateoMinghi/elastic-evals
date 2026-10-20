package eeopensearchexporter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer/consumererror"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.uber.org/zap"
)

type osExporter struct {
	cfg    *Config
	set    exporter.Settings
	client *http.Client
	prices priceBook
	logger *zap.Logger
	now    func() time.Time
}

func newExporter(cfg *Config, set exporter.Settings) *osExporter {
	return &osExporter{cfg: cfg, set: set, logger: set.Logger, now: time.Now}
}

func (e *osExporter) start(ctx context.Context, host component.Host) error {
	client, err := e.cfg.HTTP.ToClient(ctx, host.GetExtensions(), e.set.TelemetrySettings)
	if err != nil {
		return err
	}
	e.client = client
	e.prices, err = loadPrices(e.cfg.PricesFile)
	return err
}

func (e *osExporter) pushTraces(ctx context.Context, td ptrace.Traces) error {
	items := e.buildDocs(td, e.now())
	if len(items) == 0 {
		return nil
	}
	var body bytes.Buffer
	enc := json.NewEncoder(&body)
	for _, it := range items {
		meta := map[string]any{"_index": e.cfg.Prefix + "-" + it.Kind + "-" + it.Pool, "_id": it.ID, "routing": it.Routing}
		if err := enc.Encode(map[string]any{"create": meta}); err != nil {
			return consumererror.NewPermanent(err)
		}
		if err := enc.Encode(it.Doc); err != nil {
			return consumererror.NewPermanent(err)
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(e.cfg.HTTP.Endpoint, "/")+"/_bulk", &body)
	if err != nil {
		return consumererror.NewPermanent(err)
	}
	req.Header.Set("Content-Type", "application/x-ndjson")
	if e.cfg.Username != "" {
		req.SetBasicAuth(e.cfg.Username, e.cfg.Password)
	}
	resp, err := e.client.Do(req)
	if err != nil {
		return err // transport errors are retried by exporterhelper; create + fixed ids keep it idempotent
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
		return fmt.Errorf("bulk: %d %s", resp.StatusCode, truncate(string(raw), 300))
	}
	if resp.StatusCode != http.StatusOK {
		return consumererror.NewPermanent(fmt.Errorf("bulk: %d %s", resp.StatusCode, truncate(string(raw), 300)))
	}
	return e.checkItems(raw, len(items))
}

type bulkResponse struct {
	Errors bool `json:"errors"`
	Items  []map[string]struct {
		Status int             `json:"status"`
		Error  json.RawMessage `json:"error"`
	} `json:"items"`
}

// checkItems: 409 means the document was already written (re-delivery). 429 and 5xx items make
// the whole batch retry, which is safe for the same reason. Anything else (a mapping error) is
// dropped and logged: retrying cannot fix it.
func (e *osExporter) checkItems(raw []byte, n int) error {
	var r bulkResponse
	if err := json.Unmarshal(raw, &r); err != nil {
		return fmt.Errorf("bulk response: %w", err)
	}
	if !r.Errors {
		return nil
	}
	retry, dropped := 0, 0
	var first string
	for _, it := range r.Items {
		for _, res := range it {
			switch {
			case res.Status == 409 || res.Status < 300:
			case res.Status == 429 || res.Status >= 500:
				retry++
			default:
				dropped++
				if first == "" {
					first = string(res.Error)
				}
			}
		}
	}
	if dropped > 0 {
		e.logger.Warn("dropped documents rejected by OpenSearch", zap.Int("dropped", dropped), zap.Int("batch", n),
			zap.String("first_error", truncate(first, 500)))
	}
	if retry > 0 {
		return fmt.Errorf("bulk: %d of %d documents throttled or failed", retry, n)
	}
	return nil
}
