package eeopensearchexporter

import (
	"testing"

	"go.opentelemetry.io/collector/confmap"
)

func TestConfigUnmarshal(t *testing.T) {
	cm := confmap.NewFromStringMap(map[string]any{
		"endpoint": "http://os:9200", "prefix": "t1", "pools": map[string]any{"acme": "acme"},
		"retry_on_failure": map[string]any{"enabled": true, "max_elapsed_time": "0s"}, "timeout": "7s",
	})
	cfg := createDefaultConfig().(*Config)
	if err := cm.Unmarshal(cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.Prefix != "t1" || cfg.Pools["acme"] != "acme" || cfg.HTTP.Endpoint != "http://os:9200" || cfg.MaxElapsedTime != 0 {
		t.Fatalf("%+v", cfg)
	}
	if cfg.TimeoutConfig.Timeout.Seconds() != 7 {
		t.Fatalf("timeout %v", cfg.TimeoutConfig.Timeout)
	}
}
