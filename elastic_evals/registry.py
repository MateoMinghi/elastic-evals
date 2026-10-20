"""Suites, calibrations and their lifecycle, stored in Postgres (DESIGN.md §5.4, §6.3 step 6).

    candidate --start_shadow--> shadow --promote--> active --(next promotion)--> retired
        \\_____________________ reject ______________________/

Suite versions and calibrations are immutable; only a calibration's status changes. Local heads
are pickled into the artifact store and referenced by URI.
"""

import json
import os
import pickle
from pathlib import Path

from .evaluators import LocalHeadEvaluator, build
from .store.postgres import Jsonb, Postgres
from .suite import Suite


class PolicyViolation(RuntimeError):
    pass


class ArtifactStore:
    """file:///dir or s3://bucket/prefix."""

    def __init__(self, root: str | None = None):
        self.root = (root or os.environ.get("EE_ARTIFACTS", "file://" + str(Path.cwd() / ".artifacts"))).rstrip("/")

    def put(self, name: str, data: bytes) -> str:
        uri = f"{self.root}/{name}"
        if uri.startswith("file://"):
            path = Path(uri[len("file://"):])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        elif uri.startswith("s3://"):
            import boto3
            bucket, key = uri[5:].split("/", 1)
            boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=data)
        else:
            raise ValueError(f"unsupported artifact store {self.root}")
        return uri

    @staticmethod
    def get(uri: str) -> bytes:
        if uri.startswith("file://"):
            return Path(uri[len("file://"):]).read_bytes()
        if uri.startswith("s3://"):
            import boto3
            bucket, key = uri[5:].split("/", 1)
            return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        raise ValueError(f"unsupported artifact uri {uri}")


class Registry:
    def __init__(self, pg: Postgres, artifacts: ArtifactStore | None = None):
        self.pg, self.artifacts = pg, artifacts or ArtifactStore()
        self._engines = {}

    # -- tenants ---------------------------------------------------------------------------------
    def ensure_tenant(self, tenant_id: str, name: str | None = None, tier: str = "pooled",
                      index_pool: str | None = None, allow_hosted: bool = True):
        self.pg.exec("""INSERT INTO tenants (id, name, tier, index_pool, allow_hosted) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET tier = EXCLUDED.tier, index_pool = EXCLUDED.index_pool,
                        allow_hosted = EXCLUDED.allow_hosted""",
                     tenant_id, name or tenant_id, tier, index_pool or ("shared" if tier == "pooled" else tenant_id),
                     allow_hosted)

    def tenant(self, tenant_id: str) -> dict:
        t = self.pg.one("SELECT * FROM tenants WHERE id = %s", tenant_id)
        if t is None:
            raise KeyError(f"unknown tenant {tenant_id!r}")
        return t

    def pool(self, tenant_id: str) -> str:
        t = self.pg.one("SELECT index_pool FROM tenants WHERE id = %s", tenant_id)
        return t["index_pool"] if t else "shared"

    # -- suites ----------------------------------------------------------------------------------
    def put_suite(self, tenant_id: str, suite_id: str, yaml_text: str, agent_name: str | None = None) -> str:
        suite = Suite.from_yaml(yaml_text)
        with self.pg.tx() as c:
            c.execute("""INSERT INTO suites (tenant_id, id, agent_name) VALUES (%s, %s, %s)
                         ON CONFLICT (tenant_id, id) DO UPDATE SET agent_name = EXCLUDED.agent_name""",
                      (tenant_id, suite_id, agent_name))
            c.execute("""INSERT INTO suite_versions (tenant_id, suite_id, version, yaml) VALUES (%s, %s, %s, %s)
                         ON CONFLICT DO NOTHING""", (tenant_id, suite_id, suite.version, yaml_text))
            for e in suite.evaluators:
                c.execute("INSERT INTO evaluators (tenant_id, id, kind) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                          (tenant_id, e["id"], e["type"]))
        self.pg.audit("system", "suite.put", f"{suite_id}@{suite.version}", tenant_id)
        return suite.version

    def suite(self, tenant_id: str, suite_id: str, version: str) -> Suite:
        row = self.pg.one("SELECT yaml FROM suite_versions WHERE tenant_id=%s AND suite_id=%s AND version=%s",
                          tenant_id, suite_id, version)
        return Suite.from_yaml(row["yaml"])

    def suites_for(self, tenant_id: str, agent_name: str | None) -> list[str]:
        rows = self.pg.all("SELECT id FROM suites WHERE tenant_id=%s AND (agent_name IS NULL OR agent_name=%s) ORDER BY id",
                           tenant_id, agent_name)
        return [r["id"] for r in rows]

    # -- calibrations ----------------------------------------------------------------------------
    def save_calibration(self, tenant_id: str, suite_id: str, cal: dict, evaluators, label_set_id: str | None = None,
                         actor: str = "system") -> str:
        heads = {e.id: e.heads for e in evaluators if isinstance(e, LocalHeadEvaluator)}
        uri = self.artifacts.put(f"{tenant_id}/{suite_id}/{cal['id']}/heads.pkl", pickle.dumps(heads)) if heads else None
        self.pg.exec("""INSERT INTO calibrations (id, tenant_id, suite_id, suite_version, label_set_id, status, policy,
                        routes, thresholds, hosted_set, report, artifact_uri)
                        VALUES (%s, %s, %s, %s, %s, 'candidate', %s, %s, %s, %s, %s, %s)""",
                     cal["id"], tenant_id, suite_id, cal["suite_version"], label_set_id, Jsonb(cal["policy"]),
                     Jsonb(cal["routes"]), Jsonb(cal["thresholds"]), cal["suite_choice"]["hosted"],
                     Jsonb(json.loads(json.dumps(cal, default=str))), uri)
        self.pg.audit(actor, "calibration.create", cal["id"], tenant_id, suite=suite_id)
        return cal["id"]

    def calibration(self, cal_id: str) -> dict:
        row = self.pg.one("SELECT * FROM calibrations WHERE id = %s", cal_id)
        if row is None:
            raise KeyError(f"unknown calibration {cal_id!r}")
        return row

    def calibrations(self, tenant_id: str, suite_id: str | None = None) -> list[dict]:
        return self.pg.all("""SELECT id, suite_id, suite_version, status, hosted_set, created_at, promoted_at, retired_at
                              FROM calibrations WHERE tenant_id=%s AND (%s::text IS NULL OR suite_id=%s)
                              ORDER BY created_at DESC""", tenant_id, suite_id, suite_id)

    def by_status(self, tenant_id: str, suite_id: str, status: str) -> dict | None:
        return self.pg.one("SELECT * FROM calibrations WHERE tenant_id=%s AND suite_id=%s AND status=%s",
                           tenant_id, suite_id, status)

    def start_shadow(self, cal_id: str, actor: str = "system"):
        cal = self.calibration(cal_id)
        if cal["status"] != "candidate":
            raise ValueError(f"{cal_id} is {cal['status']}, not candidate")
        with self.pg.tx() as c:
            c.execute("UPDATE calibrations SET status='rejected', retired_at=now() WHERE tenant_id=%s AND suite_id=%s AND status='shadow'",
                      (cal["tenant_id"], cal["suite_id"]))
            c.execute("UPDATE calibrations SET status='shadow' WHERE id=%s", (cal_id,))
        self.pg.audit(actor, "calibration.shadow", cal_id, cal["tenant_id"])

    def promote(self, cal_id: str, baseline_agreement: dict | None = None, actor: str = "system"):
        """Make a calibration active; the previous active one is retired. The drift baseline is the
        audit agreement measured in shadow if given, else the calibration-time estimate."""
        cal = self.calibration(cal_id)
        if cal["status"] not in ("candidate", "shadow"):
            raise ValueError(f"{cal_id} is {cal['status']}")
        self._check_tenant_policy(cal)
        baseline = baseline_agreement or cal["report"].get("audit_agreement") or {}
        with self.pg.tx() as c:
            c.execute("UPDATE calibrations SET status='retired', retired_at=now() WHERE tenant_id=%s AND suite_id=%s AND status='active'",
                      (cal["tenant_id"], cal["suite_id"]))
            c.execute("UPDATE calibrations SET status='active', promoted_at=now(), baseline_agreement=%s WHERE id=%s",
                      (Jsonb(baseline), cal_id))
        self.pg.audit(actor, "calibration.promote", cal_id, cal["tenant_id"])

    def reject(self, cal_id: str, actor: str = "system"):
        cal = self.calibration(cal_id)
        self.pg.exec("UPDATE calibrations SET status='rejected', retired_at=now() WHERE id=%s AND status IN ('candidate','shadow')", cal_id)
        self.pg.audit(actor, "calibration.reject", cal_id, cal["tenant_id"])

    def _check_tenant_policy(self, cal):
        tenant = self.tenant(cal["tenant_id"])
        if not tenant["allow_hosted"] and (cal["hosted_set"] or cal["policy"].get("allow_hosted")):
            raise PolicyViolation(f"tenant {tenant['id']} is local-only; calibration {cal['id']} uses hosted evaluators")

    # -- engines ---------------------------------------------------------------------------------
    def engine(self, cal_id: str, client=None):
        """(suite, calibration document, evaluators) for a calibration, cached by id (immutable)."""
        if cal_id not in self._engines:
            row = self.calibration(cal_id)
            self._check_tenant_policy(row)
            suite = self.suite(row["tenant_id"], row["suite_id"], row["suite_version"])
            suite.policy.allow_hosted = bool(row["policy"].get("allow_hosted", True))
            if suite.policy.allow_hosted and client is None:
                from .providers import OpenRouter
                client = OpenRouter()
            evaluators = build(suite, client)
            heads = pickle.loads(self.artifacts.get(row["artifact_uri"])) if row["artifact_uri"] else {}
            for e in evaluators:
                if isinstance(e, LocalHeadEvaluator):
                    e.heads = heads.get(e.id, {})
            self._engines[cal_id] = (suite, row["report"], evaluators)
        return self._engines[cal_id]
