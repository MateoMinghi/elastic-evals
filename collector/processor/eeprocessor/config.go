// Package eeprocessor tags spans with their tenant, normalises OpenInference, OpenLLMetry and
// Langfuse attributes onto the OpenTelemetry GenAI semantic conventions, and marks spans from
// internal environments (evaluator traffic) so they are never evaluated (DESIGN.md §5.1, §5.2).
package eeprocessor

import (
	"errors"

	"go.opentelemetry.io/collector/component"
)

// TenantConfig says where a span's tenant comes from, in priority order.
type TenantConfig struct {
	// Resource attribute set by the agent (or by an upstream collector). Default "ee.tenant_id".
	FromResourceAttribute string `mapstructure:"from_resource_attribute"`
	// Client metadata key (a request header; needs include_metadata on the receiver), e.g. "x-ee-tenant".
	FromMetadata string `mapstructure:"from_metadata"`
	// Fallback when neither is present.
	Default string `mapstructure:"default"`
	// Drop spans whose tenant cannot be determined.
	Require bool `mapstructure:"require"`
}

// Config of the elasticevals processor.
type Config struct {
	Tenant TenantConfig `mapstructure:"tenant"`
	// Map OpenInference / OpenLLMetry / Langfuse / legacy gen_ai attributes onto current gen_ai.*.
	Normalize bool `mapstructure:"normalize"`
	// deployment.environment values whose spans are internal (evaluator calls); they get ee.internal=true.
	InternalEnvironments []string `mapstructure:"internal_environments"`
	// Drop gen_ai content attributes (messages, tool arguments and results) instead of forwarding them.
	DropContent bool `mapstructure:"drop_content"`
	// Redact content with a Presidio analyzer before it leaves the gateway.
	Presidio PresidioConfig `mapstructure:"presidio"`
}

var _ component.Config = (*Config)(nil)

// Validate checks the configuration.
func (c *Config) Validate() error {
	if c.Tenant.Require && c.Tenant.Default != "" {
		return errors.New("tenant.require and tenant.default are mutually exclusive")
	}
	if c.Presidio.OnError != "drop_content" && c.Presidio.OnError != "keep" {
		return errors.New(`presidio.on_error must be "drop_content" or "keep"`)
	}
	return nil
}

func createDefaultConfig() component.Config {
	return &Config{
		Tenant:               TenantConfig{FromResourceAttribute: "ee.tenant_id"},
		Normalize:            true,
		InternalEnvironments: []string{"ee-internal"},
		Presidio:             defaultPresidio(),
	}
}
