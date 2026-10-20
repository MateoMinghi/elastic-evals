package eeopensearchexporter

import (
	"context"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/exporter/exporterhelper"
)

// Type is the component type used in collector configuration.
var Type = component.MustNewType("elasticevals_opensearch")

// NewFactory returns the exporter factory.
func NewFactory() exporter.Factory {
	return exporter.NewFactory(Type, createDefaultConfig,
		exporter.WithTraces(createTraces, component.StabilityLevelDevelopment))
}

func createTraces(ctx context.Context, set exporter.Settings, cfg component.Config) (exporter.Traces, error) {
	c := cfg.(*Config)
	e := newExporter(c, set)
	return exporterhelper.NewTraces(ctx, set, cfg, e.pushTraces,
		exporterhelper.WithStart(e.start),
		exporterhelper.WithTimeout(c.TimeoutConfig),
		exporterhelper.WithRetry(c.BackOffConfig),
		exporterhelper.WithCapabilities(consumer.Capabilities{MutatesData: false}))
}
