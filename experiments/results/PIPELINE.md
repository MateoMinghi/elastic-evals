# The engine through the production path

Calibration: 158 set-A conversations (as in ENGINE_SUMMARY.md). Holdout: the 300 set-C conversations, replayed as OpenTelemetry spans through gateway collector → Kafka → ingest collector → OpenSearch → tracker → evaluation worker. Hosted answers come from the cache, so any change in what evaluators see would have failed the run.

- Conversations evaluated: **300 / 300**, errors: 0
- Spans: 4149 sent, 4149 `obs` documents, 4149 staged content documents
- Hosted set chosen by calibration: jev, llm_haiku
- Timing (one machine, 4 cores): spans indexed 3.5s after the replay started; all conversations scored 49s later (session idle timeout 3s; hosted answers from cache)
- Evaluation cost includes the 2% audit sample
- Evaluation cost: $85 per 1M conversations; sent to an LLM: 36%
- Content kept after evaluation (eval-aware retention): 280 / 300 (audit: 3, error: 16, flagged: 276, low_confidence: 121, random: 1, tier0_failure: 173)

| check | through the pipeline % | engine study % |
|---|---|---|
| false_confirmation | 94.3 | 94.3 |
| disclosure_missing | 99.3 | 99.3 |
| fee_mismatch | 93.3 | 93.3 |
| user_frustrated | 80.7 | 80.7 |
| tool_loop | 100.0 | 100.0 |
| **mean** | **93.5** | **93.5** |

Drift monitor on the audit sample (agreement of routed decisions with the audit judge):

| check | n | live agreement % | calibration baseline % |
|---|---|---|---|
| disclosure_missing | 3 | 100.0 | 100.0 |
| false_confirmation | 3 | 100.0 | 98.7 |
| fee_mismatch | 3 | 100.0 | 98.7 |
| tool_loop | 3 | 100.0 | 100.0 |
| user_frustrated | 3 | 66.7 | 88.0 |
