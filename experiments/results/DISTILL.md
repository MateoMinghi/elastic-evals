# Distillation: LLM labels train the local heads

Labelled: 158 set-A conversations. Unlabelled pool: 300 set-B conversations, labelled by `llm_haiku` (the suite's routing judge). Test: the 300 set-C holdout. Hosted answers from cache.

| run | false_confirmation | disclosure_missing | fee_mismatch | user_frustrated | tool_loop | mean % | $ / 1M conv | ms / conv | sent to an LLM |
|---|---|---|---|---|---|---|---|---|---|
| hosted: labelled only | 94.3 | 99.3 | 93.3 | 80.7 | 100.0 | **93.5** | 83 | 554 | 36% |
| hosted: + distilled from llm_haiku on 300 unlabelled | 94.7 | 99.3 | 93.3 | 82.0 | 100.0 | **93.9** | 53 | 587 | 20% |
| local-only: labelled only | 82.7 | 99.3 | 82.3 | 72.0 | 100.0 | **87.3** | 0 | 43 | 0% |
| local-only: + distilled from llm_haiku on 300 unlabelled | 85.7 | 99.3 | 82.3 | 74.3 | 100.0 | **88.3** | 0 | 33 | 0% |

Routes:

- **hosted: labelled only**: false_confirmation: rules → e5 → jev → llm_haiku; disclosure_missing: rules; fee_mismatch: jev → llm_haiku; user_frustrated: e5 → jev → llm_haiku; tool_loop: rules
- **hosted: + distilled from llm_haiku on 300 unlabelled**: false_confirmation: rules → e5 → jev → llm_luna; disclosure_missing: rules; fee_mismatch: jev → llm_luna; user_frustrated: e5 → jev; tool_loop: rules
- **local-only: labelled only**: false_confirmation: rules → e5; disclosure_missing: rules; fee_mismatch: rules; user_frustrated: e5; tool_loop: rules
- **local-only: + distilled from llm_haiku on 300 unlabelled**: false_confirmation: rules → e5; disclosure_missing: rules; fee_mismatch: rules; user_frustrated: e5; tool_loop: rules

Teacher agreement with the planted labels on the pool: false_confirmation 97.3%, disclosure_missing 99.7%, fee_mismatch 98.3%, user_frustrated 83.0%, tool_loop 92.3%
