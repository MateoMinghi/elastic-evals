package eeprocessor

import (
	"context"
	"slices"

	"go.opentelemetry.io/collector/client"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.uber.org/zap"
)

const (
	attrTenant   = "ee.tenant_id"
	attrInternal = "ee.internal"
	attrSource   = "ee.semconv.source"
)

type eeProcessor struct {
	cfg    *Config
	logger *zap.Logger
}

func newProcessor(cfg *Config, logger *zap.Logger) *eeProcessor {
	return &eeProcessor{cfg: cfg, logger: logger}
}

func (p *eeProcessor) processTraces(ctx context.Context, td ptrace.Traces) (ptrace.Traces, error) {
	fromMeta := ""
	if p.cfg.Tenant.FromMetadata != "" {
		if v := client.FromContext(ctx).Metadata.Get(p.cfg.Tenant.FromMetadata); len(v) > 0 {
			fromMeta = v[0]
		}
	}
	td.ResourceSpans().RemoveIf(func(rs ptrace.ResourceSpans) bool {
		res := rs.Resource().Attributes()
		tenant := p.tenant(res, fromMeta)
		if tenant == "" {
			if p.cfg.Tenant.Require {
				p.logger.Debug("dropping spans without tenant", zap.Int("spans", countSpans(rs)))
				return true
			}
		} else {
			res.PutStr(attrTenant, tenant)
		}
		internal := slices.Contains(p.cfg.InternalEnvironments, environment(res))
		for _, ss := range rs.ScopeSpans().All() {
			for _, span := range ss.Spans().All() {
				attrs := span.Attributes()
				if p.cfg.Normalize {
					Normalize(attrs)
				}
				if p.cfg.DropContent {
					dropContent(attrs)
				}
				if internal {
					attrs.PutBool(attrInternal, true)
				}
			}
		}
		return false
	})
	return td, nil
}

func (p *eeProcessor) tenant(res pcommon.Map, fromMeta string) string {
	if p.cfg.Tenant.FromResourceAttribute != "" {
		if v, ok := res.Get(p.cfg.Tenant.FromResourceAttribute); ok && v.AsString() != "" {
			return v.AsString()
		}
	}
	if fromMeta != "" {
		return fromMeta
	}
	return p.cfg.Tenant.Default
}

func environment(res pcommon.Map) string {
	for _, k := range []string{"deployment.environment.name", "deployment.environment"} {
		if v, ok := res.Get(k); ok {
			return v.AsString()
		}
	}
	return ""
}

func countSpans(rs ptrace.ResourceSpans) int {
	n := 0
	for _, ss := range rs.ScopeSpans().All() {
		n += ss.Spans().Len()
	}
	return n
}

// ContentAttributes are the gen_ai attributes that carry conversation text.
var ContentAttributes = []string{
	"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
	"gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
}

func dropContent(attrs pcommon.Map) {
	for _, k := range ContentAttributes {
		attrs.Remove(k)
	}
}
