// Package eeopensearchexporter writes spans to the elastic-evals OpenSearch layout (DESIGN.md §5.3):
// one immutable `obs` document per span (typed columns, truncated previews, cost) and, when the
// span carries conversation content, one `stage` document with the full content. Staged content
// lives a few days; the evaluation worker copies what the retention decision keeps into `payload`.
//
// Writes use op_type=create with deterministic ids and routing by trace id, so re-delivery from
// Kafka or a retried bulk request is idempotent.
package eeopensearchexporter

import (
	"errors"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/config/confighttp"
	"go.opentelemetry.io/collector/config/configretry"
	"go.opentelemetry.io/collector/exporter/exporterhelper"
)

// Config of the elasticevals_opensearch exporter.
type Config struct {
	// Named, not embedded: an embedded ClientConfig promotes its Unmarshal method, which then
	// decodes only the HTTP fields and silently ignores ours.
	HTTP confighttp.ClientConfig `mapstructure:",squash"`
	// Index prefix; aliases are {prefix}-{kind}-{pool}.
	Prefix string `mapstructure:"prefix"`
	// Tenants with a dedicated index pool: tenant id -> pool name. Everyone else is in "shared".
	Pools map[string]string `mapstructure:"pools"`
	// Characters kept in obs input/output previews.
	PreviewChars int `mapstructure:"preview_chars"`
	// "stage": write full content to the staging index; "none": drop it.
	Content string `mapstructure:"content"`
	// JSON file with model prices (same shape as the model_prices table). Empty: no cost.
	PricesFile string `mapstructure:"prices_file"`
	// Basic auth for the OpenSearch security plugin.
	Username string `mapstructure:"username"`
	Password string `mapstructure:"password"`

	exporterhelper.TimeoutConfig `mapstructure:",squash"`
	configretry.BackOffConfig    `mapstructure:"retry_on_failure"`
}

var _ component.Config = (*Config)(nil)

// Validate checks the configuration.
func (c *Config) Validate() error {
	if c.HTTP.Endpoint == "" {
		return errors.New("endpoint is required")
	}
	if c.Content != "stage" && c.Content != "none" {
		return errors.New(`content must be "stage" or "none"`)
	}
	return nil
}

func createDefaultConfig() component.Config {
	client := confighttp.NewDefaultClientConfig()
	client.Endpoint = "http://localhost:9200"
	return &Config{
		HTTP:          client,
		Prefix:        "ee",
		PreviewChars:  200,
		Content:       "stage",
		TimeoutConfig: exporterhelper.NewDefaultTimeoutConfig(),
		BackOffConfig: configretry.NewDefaultBackOffConfig(),
	}
}
