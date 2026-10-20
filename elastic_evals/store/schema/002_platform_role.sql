-- Tenant admins administer their own tenant. Global scopes (creating tenants, keys for another tenant
-- or platform keys, the shared price book) belong to the platform role only.
INSERT INTO roles (id, scopes) VALUES ('platform', ARRAY['*:*']) ON CONFLICT (id) DO NOTHING;
UPDATE roles SET scopes = ARRAY['traces:*', 'scores:*', 'suites:*', 'calibrations:*', 'datasets:*', 'experiments:*',
                                'annotations:*', 'keys:write', 'audit:read', 'prices:read']
WHERE id = 'admin';
