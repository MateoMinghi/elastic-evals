-- elastic-evals configuration store (DESIGN.md §5.4). Everything versioned, transactional or small.
-- High-volume, append-only data lives in OpenSearch.

CREATE TABLE tenants (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    tier          text NOT NULL DEFAULT 'pooled' CHECK (tier IN ('pooled', 'dedicated')),
    index_pool    text NOT NULL DEFAULT 'shared',       -- OpenSearch index pool: 'shared' or the tenant's own
    allow_hosted  boolean NOT NULL DEFAULT true,        -- false: local-only, overrides suite policy
    retention     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- per-kind retention overrides, e.g. {"payload_days": 30}
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id          text PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE roles (
    id      text PRIMARY KEY,                          -- e.g. 'admin', 'annotator', 'viewer'
    scopes  text[] NOT NULL                            -- 'resource:action', e.g. 'scores:read', '*:*'
);

CREATE TABLE memberships (
    tenant_id  text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id    text NOT NULL,
    role_id    text NOT NULL REFERENCES roles(id),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE api_keys (
    id          text PRIMARY KEY,                      -- public part
    tenant_id   text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    secret_hash text NOT NULL,                         -- sha256 of the secret part
    role_id     text NOT NULL REFERENCES roles(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

CREATE TABLE suites (
    id          text NOT NULL,
    tenant_id   text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_name  text,                                  -- which agent's conversations this suite scores (null: all)
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE suite_versions (
    tenant_id   text NOT NULL,
    suite_id    text NOT NULL,
    version     text NOT NULL,                          -- sha256 prefix of the YAML
    yaml        text NOT NULL,                          -- immutable
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, suite_id, version),
    FOREIGN KEY (tenant_id, suite_id) REFERENCES suites(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE evaluators (
    tenant_id   text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id          text NOT NULL,
    kind        text NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE evaluator_versions (
    tenant_id   text NOT NULL,
    evaluator_id text NOT NULL,
    version     text NOT NULL,                          -- pinned model id or code/weights digest, never '-latest'
    config      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, evaluator_id, version),
    FOREIGN KEY (tenant_id, evaluator_id) REFERENCES evaluators(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE label_sets (
    id          text PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    suite_id    text NOT NULL,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE labels (
    label_set_id    text NOT NULL REFERENCES label_sets(id) ON DELETE CASCADE,
    conversation_id text NOT NULL,
    check_name      text NOT NULL,
    value           text NOT NULL,                      -- '0' / '1' for binary checks, the class for multi-class
    source          text NOT NULL CHECK (source IN ('human', 'synthetic', 'judge')),
    annotator       text,
    lang            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (label_set_id, conversation_id, check_name, source)
);

CREATE TABLE calibrations (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    suite_id        text NOT NULL,
    suite_version   text NOT NULL,
    label_set_id    text REFERENCES label_sets(id),
    status          text NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate', 'shadow', 'active', 'retired', 'rejected')),
    policy          jsonb NOT NULL,
    routes          jsonb NOT NULL,
    thresholds      jsonb NOT NULL,
    hosted_set      text[] NOT NULL,
    report          jsonb NOT NULL,                     -- the full calibration document
    artifact_uri    text,                               -- local heads (pickle) in S3 / the artifact store
    baseline_agreement jsonb,                           -- audit agreement per check at promotion time
    created_at      timestamptz NOT NULL DEFAULT now(),
    promoted_at     timestamptz,
    retired_at      timestamptz,
    FOREIGN KEY (tenant_id, suite_id, suite_version) REFERENCES suite_versions(tenant_id, suite_id, version)
);
-- at most one active and one shadow calibration per suite
CREATE UNIQUE INDEX calibrations_one_active ON calibrations (tenant_id, suite_id) WHERE status = 'active';
CREATE UNIQUE INDEX calibrations_one_shadow ON calibrations (tenant_id, suite_id) WHERE status = 'shadow';

CREATE TABLE datasets (
    id          text PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        text NOT NULL,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE dataset_items (
    id              text NOT NULL,
    dataset_id      text NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    input           jsonb NOT NULL,                     -- conversation so far / task input
    expected        jsonb,                              -- expected labels, tool sequence, output
    source_conversation_id text,
    valid_from      timestamptz NOT NULL DEFAULT now(),
    valid_to        timestamptz,                        -- null: current version
    PRIMARY KEY (dataset_id, id, valid_from)
);

CREATE TABLE experiment_runs (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    dataset_id      text NOT NULL REFERENCES datasets(id),
    name            text NOT NULL,
    suite_id        text,
    calibration_id  text,
    baseline_run_id text REFERENCES experiment_runs(id),
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary         jsonb,                              -- per-check rates and gate results
    status          text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'passed', 'failed', 'error')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

CREATE TABLE annotation_queues (
    id          text PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    suite_id    text NOT NULL,
    name        text NOT NULL,
    checks      text[] NOT NULL,                        -- checks to label
    label_set_id text NOT NULL REFERENCES label_sets(id),
    fill_rules  jsonb NOT NULL DEFAULT '[]'::jsonb,     -- conditions that add items automatically
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE annotation_items (
    queue_id        text NOT NULL REFERENCES annotation_queues(id) ON DELETE CASCADE,
    conversation_id text NOT NULL,
    reason          text NOT NULL,                      -- audit_disagreement, tier_disagreement, low_confidence, stratified
    priority        real NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'locked', 'done', 'skipped')),
    locked_by       text,
    locked_at       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (queue_id, conversation_id)
);
CREATE INDEX annotation_items_next ON annotation_items (queue_id, status, priority DESC, created_at);

CREATE TABLE model_prices (
    id              serial PRIMARY KEY,
    model_pattern   text NOT NULL,                      -- regex over the response (or request) model
    start_date      date NOT NULL DEFAULT '1970-01-01',
    tiers           jsonb NOT NULL,                     -- [{"above_input_tokens": 0, "input": usd_per_1M, "output": ..., "cache_read": ..., "cache_write": ..., "reasoning": ...}]
    priority        integer NOT NULL DEFAULT 0
);

CREATE TABLE trace_tracker (
    tenant_id       text NOT NULL,
    conversation_id text NOT NULL,                      -- trace id, or gen_ai.conversation.id for sessions
    kind            text NOT NULL DEFAULT 'trace' CHECK (kind IN ('trace', 'session')),
    agent_name      text,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_span_at    timestamptz NOT NULL,
    root_ended      boolean NOT NULL DEFAULT false,
    due_at          timestamptz NOT NULL,               -- when the conversation counts as complete
    n_spans         integer NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'claimed', 'evaluated', 'error')),
    claimed_by      text,
    claimed_at      timestamptz,
    evaluated_at    timestamptz,
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text,
    PRIMARY KEY (tenant_id, conversation_id)
);
CREATE INDEX trace_tracker_due ON trace_tracker (status, due_at);

CREATE TABLE sentry_links (
    tenant_id       text NOT NULL,
    trace_id        text NOT NULL,
    sentry_issue_id text NOT NULL,
    sentry_event_id text,
    project_slug    text,
    status          text,                               -- unresolved, resolved, ignored
    external_issue_created boolean NOT NULL DEFAULT false,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, trace_id, sentry_issue_id)
);
CREATE INDEX sentry_links_issue ON sentry_links (sentry_issue_id);

CREATE TABLE drift_alerts (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    suite_id        text NOT NULL,
    calibration_id  text NOT NULL,
    check_name      text NOT NULL,
    lang            text,
    baseline        real NOT NULL,
    observed        real NOT NULL,
    n               integer NOT NULL,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    tenant_id   text,
    actor       text NOT NULL,
    action      text NOT NULL,
    target      text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);

INSERT INTO roles (id, scopes) VALUES
    ('admin', ARRAY['*:*']),
    ('engineer', ARRAY['traces:read', 'scores:read', 'scores:write', 'suites:*', 'calibrations:*', 'datasets:*', 'experiments:*', 'annotations:*']),
    ('annotator', ARRAY['traces:read', 'scores:read', 'annotations:*']),
    ('viewer', ARRAY['traces:read', 'scores:read', 'calibrations:read', 'experiments:read']),
    ('ingest', ARRAY['ingest:write']);
