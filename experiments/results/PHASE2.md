# Phase-2 gate: 2900 spans/s for 5 minutes on a 3-node cluster

Target peak and budgets: DESIGN.md §12. Load: 68503 conversations (871906 spans) from 4 paced sender processes through gateway → Kafka (6 partitions) → ingest → OpenSearch (3 nodes, 3 primaries + 1 replica per index), while the tracker and 2 workers evaluate (rule checks only) and the UI's queries run. Everything shares one 4-core VM. Script: `experiments/phase2_gate.py`.

- Sent at **2906 spans/s** (target 2900); indexed 871906 of 871906; drained 20 s after the last send
- Ingest latency, scheduled send → searchable (3426 sampled conversations, agent SDK batching included): p50 **7.7 s**, p95 **21.7 s**, p99 24.4 s, max 26.0 s
- Storage after force merge: obs **0.34 GB** per million spans (primary), stage 0.37 GB; replicas double both
- Evaluated during the load: 6594 conversations, so the UI queries ran on that many rollups

| query, during the load | n | p50 ms | p95 ms |
|---|---|---|---|
| list (50, sorted, facets) | 133 | 78 | 253 |
| list filtered by flag | 133 | 59 | 161 |
| conversation detail | 133 | 125 | 398 |
| score summary (7 days) | 133 | 34 | 104 |
| full-text search | 133 | 494 | 948 |
| Dashboards: LLM cost per day | 133 | 17 | 90 |
| Dashboards: errors by type | 133 | 11 | 65 |
| Dashboards: latency p50/p95 by model | 133 | 32 | 130 |

| gate | result |
|---|---|
| load reached the target rate | ✓ |
| no span lost | ✓ |
| ingest latency p95 <= 30 s | ✓ |
| obs <= 1.0 GB per million spans | ✓ |
| UI queries p95 <= 1000 ms under load | ✓ |
| full-text search p95 <= 3000 ms under load | ✓ |
| Dashboards aggregations p95 <= 2000 ms under load | ✓ |

Phase-2 gate: **passed**

## All runs (same VM, same load unless noted)

| run | evaluation on this VM | Kafka partitions | sustained ingest | ingest p95 | gate |
|---|---|---|---|---|---|
| 1 min smoke | rules + e5 heads, 2 workers | 1 | ~1,700 spans/s (backlog drained 44 s after the load) | 44 s | ✗ |
| 1 min smoke | rules + e5 heads, 2 workers | 6 | kept up (drained in 16 s) | 14.5 s | ✓ |
| 5 min | rules + e5 heads, 2 workers | 6 | ~2,050 spans/s (backlog drained 125 s after the load) | 122 s | ✗ |
| 5 min | none | 6 | kept up (drained in 3 s) | 5.8 s | ✓ (UI list queries on an empty rollup index: not meaningful) |
| **5 min (above)** | **rule checks, 2 workers** | **6** | **kept up (drained in 20 s)** | **21.7 s** | **✓** |

- **Partitions are ingest's write concurrency.** Offsets are committed only after OpenSearch accepts a batch, so each partition has one bulk request in flight; with one partition the replicated cluster could not keep up. DESIGN.md §5.2 and the Helm values now say so; docker compose already used 6.
- **On one 4-core VM, the e5 heads and the cluster compete for CPU.** With local heads evaluating on the same box (they run at about 16 conversations/s on CPU, LOAD.md), the three OpenSearch nodes got under two cores and ingest fell to about 2,050 spans/s. The design already serves local heads separately (ONNX Runtime or TEI, §11) and scales workers out; with only rule checks on the box, everything stays within budget.
- **Evaluation did not keep up with 228 conversations/s** in any run (6,594 of 68,503 evaluated during the gated run). That is a capacity question for the evaluation tier, not part of this gate: workers scale horizontally on the same Kafka topic.
- One VM hosting the load generator, both collectors, Kafka, Postgres, three OpenSearch nodes and the workers is a conservative setting: a production cluster has its own nodes.
