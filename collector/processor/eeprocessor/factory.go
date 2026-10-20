package eeprocessor

import (
	"context"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/processor"
	"go.opentelemetry.io/collector/processor/processorhelper"
)

// Type is the component type used in collector configuration.
var Type = component.MustNewType("elasticevals")

// NewFactory returns the processor factory.
func NewFactory() processor.Factory {
	return processor.NewFactory(Type, createDefaultConfig,
		processor.WithTraces(createTraces, component.StabilityLevelDevelopment))
}

func createTraces(ctx context.Context, set processor.Settings, cfg component.Config, next consumer.Traces) (processor.Traces, error) {
	p := newProcessor(cfg.(*Config), set.Logger)
	return processorhelper.NewTraces(ctx, set, cfg, next, p.processTraces,
		processorhelper.WithCapabilities(consumer.Capabilities{MutatesData: true}))
}
