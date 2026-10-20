# Cross-fitted thresholds in calibration

Calibration judges candidate routes by their accuracy on the labelled set. Without cross-fitting, the confidence thresholds are tuned on the same conversations they are judged on; with it (5 folds), each conversation is decided with thresholds tuned on the other folds. The runtime always uses thresholds tuned on all labels. Hosted answers from the cache.

**Calibrated on 158 set-A conversations, scored on the 300-conversation holdout C**

| thresholds | false_confirmation | disclosure_missing | fee_mismatch | user_frustrated | tool_loop | mean % | $ / 1M conv | sent to an LLM | hosted set |
|---|---|---|---|---|---|---|---|---|---|
| crossfit=0 | 94.3 | 99.3 | 93.3 | 80.7 | 100.0 | **93.5** | 83 | 36% | jev, llm_haiku |
| crossfit=5 | 96.0 | 99.3 | 93.3 | 80.7 | 100.0 | **93.9** | 112 | 60% | jev, llm_haiku |
| crossfit=5, floor_se=0.5 | 96.0 | 99.3 | 94.0 | 80.7 | 100.0 | **94.0** | 125 | 100% | llm_haiku |

**Phase-1 gate: 5-fold cross-validation on C (engine against the best single evaluator, 1 SE)**

| thresholds | false_confirmation | disclosure_missing | fee_mismatch | user_frustrated | tool_loop | gate | $ / 1M conv | sent to an LLM |
|---|---|---|---|---|---|---|---|---|
| crossfit=0 | 96.7 vs 97.0 | 99.3 vs 99.7 | 91.3 vs 94.0 ✗ | 84.0 vs 83.7 | 100.0 vs 100.0 | not passed | 97 | 76% |
| crossfit=5 | 97.0 vs 97.0 | 99.3 vs 99.7 | 92.0 vs 94.0 ✗ | 83.3 vs 83.7 | 100.0 vs 100.0 | not passed | 92 | 70% |
| crossfit=5, floor_se=0.5 | 97.0 vs 97.0 | 99.3 vs 99.7 | 92.7 vs 94.0 | 85.0 vs 83.7 | 100.0 vs 100.0 | PASSED | 117 | 93% |
