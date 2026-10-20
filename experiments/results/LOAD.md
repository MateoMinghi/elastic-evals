# Load test (one 4-core VM)

Everything shares one 4-core VM: OpenSearch (2 GB heap), Kafka, Postgres, the gateway and ingest collectors, the tracker, the workers and the load generator. Numbers are a floor for a single node, not a capacity plan.

- Load: 5000 conversations, 63643 spans, from 3 sender processes
- Ingest: all 63643 spans indexed after 19s → **3389 spans/s** end to end (sending took 4s)
- Evaluation (rules + e5 heads, local-only, 2 workers): 5000 conversations in 304s → **16.4 conversations/s**
- Disk after force merge: `obs` **322 MB per million spans**, `stage` (full content) 324 MB per million spans, `scores` 1935 MB per million conversations

| query (through the API) | p50 ms | p95 ms |
|---|---|---|
| list (50, sorted, facets) | 26 | 31 |
| list filtered by flag | 24 | 31 |
| full-text search | 237 | 272 |
| score summary (7 days) | 11 | 17 |
| conversation detail | 34 | 52 |

| index | documents | MB |
|---|---|---|
| embeddings | 0 | 0.0 |
| metrics | 395 | 0.1 |
| obs | 63643 | 20.5 |
| payload | 63172 | 20.6 |
| scores | 25000 | 9.7 |
| stage | 63643 | 20.6 |
| traces | 5000 | 3.3 |
