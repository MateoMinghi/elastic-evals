# Storage path on a 3-node OpenSearch cluster

Three OpenSearch 3.9 nodes (768 MB heap each) on one VM; `obs` and `stage` with 3 primaries and 1 replica; ISM rolls the write alias over at 300 kB per primary (to force rollovers on a small run). 3000 conversations through gateway → Kafka → ingest → cluster; one node killed after half of them and started again 20 s after the last span was sent.

- Spans sent: **38181**; indexed: **38181**; distinct span ids: 38184 (cardinality estimate)
- Lost: **0**; duplicates: none possible within an index (create with fixed ids); across rollover, readers dedupe
- Rollover: yes; indices cl-obs-shared-000001 (38181), cl-obs-shared-000002 (0); write index `cl-obs-shared-000002`
- Cluster status not green for about 66 s while the node was down; final status **green** with 3 nodes
- Shards per node at the end: n1: 4, n2: 4, n3: 4

| t (s) | event | cluster status |
|---|---|---|
| 3.8 | node killed | green |
| 7.9 | all spans sent | yellow |
| 27.9 | node restarted | yellow |
| 145.9 | end | green |
