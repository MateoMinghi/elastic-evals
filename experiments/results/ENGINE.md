# Engine evaluation

Suite: `banking_suite.yaml`. Targets and tolerance from the suite policy.

### hosted: calibrated on 158 labelled conversations

Hosted evaluators chosen for the suite: jev, llm_haiku (dev: $71.9 / 1M conv, 639 ms / conv, 0 question(s) below their floor).

| question           | chosen route                 |   dev acc % |   best dev acc % | meets target   |
|--------------------|------------------------------|-------------|------------------|----------------|
| false_confirmation | rules → e5 → jev → llm_haiku |        98.7 |             99.4 | yes            |
| disclosure_missing | rules                        |       100   |            100   | yes            |
| fee_mismatch       | jev → llm_haiku              |       100   |            100   | yes            |
| user_frustrated    | e5 → jev → llm_haiku         |        94.3 |             94.9 | **no**         |
| tool_loop          | rules                        |       100   |            100   | yes            |

**A-test** (237 conversations)

Drift monitor (agreement of routed decisions with the audit judge on the audited sample): disclosure_missing 100% (n=17), tool_loop 94% (n=17), fee_mismatch 100% (n=17), false_confirmation 94% (n=17), user_frustrated 94% (n=17)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 98.7 |                100   |          100   |              92.8 |       100   | **98.3** |                                  |          58.5 |         379 | 17%                    |
| rules      |                 77.2 |                100   |           84   |              62.9 |       100   | 84.8     | +11.6 … +15.4                    |           0   |           2 | 0%                     |
| e5         |                 80.2 |                 99.6 |           51.1 |              94.1 |        85.7 | 82.1     | +14.2 … +18.5                    |           0   |         203 | 0%                     |
| jev        |                 87.3 |                100   |          100   |              91.1 |       100   | 95.7     | +1.6 … +3.6                      |          37.6 |         190 | 0%                     |
| llm_ds     |                 98.7 |                100   |           99.2 |              87.3 |        98.7 | 96.8     | +0.6 … +2.4                      |          74.7 |        1200 | 100%                   |
| llm_luna   |                 99.6 |                100   |          100   |              90.7 |        87.3 | 95.5     | +1.8 … +3.8                      |          83.2 |        2419 | 100%                   |
| llm_haiku  |                 99.2 |                 99.6 |          100   |              87.8 |        94.9 | 96.3     | +1.2 … +2.9                      |         124.5 |         888 | 100%                   |

**B (Gemini; used while iterating)** (300 conversations)

Drift monitor (agreement of routed decisions with the audit judge on the audited sample): disclosure_missing 93% (n=27), tool_loop 100% (n=27), false_confirmation 100% (n=27), fee_mismatch 100% (n=27), user_frustrated 78% (n=27)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 94   |                 96   |           98.3 |              82.3 |       100   | **94.1** |                                  |          74.9 |         545 | 35%                    |
| rules      |                 81   |                 96   |           85   |              57.7 |       100   | 83.9     | +8.5 … +11.9                     |           0   |           2 | 0%                     |
| e5         |                 83   |                 75   |           53.7 |              76.7 |        93.3 | 76.3     | +15.7 … +20.0                    |           0   |         156 | 0%                     |
| jev        |                 86.3 |                100   |           94.7 |              85.7 |       100   | 93.3     | -0.6 … +2.2                      |          34.4 |         175 | 0%                     |
| llm_ds     |                 98.7 |                 99.7 |           99   |              80.7 |        98.3 | 95.3     | -2.5 … +0.2                      |         112.5 |        1172 | 100%                   |
| llm_luna   |                 97   |                 99.7 |           99.3 |              86   |        92   | 94.8     | -1.9 … +0.7                      |          76.8 |        1898 | 100%                   |
| llm_haiku  |                 97.3 |                 99.7 |           98.3 |              83   |        92.3 | 94.1     | -1.1 … +1.1                      |         115.7 |         948 | 100%                   |

**C (Qwen; final holdout)** (300 conversations)

Drift monitor (agreement of routed decisions with the audit judge on the audited sample): false_confirmation 96% (n=28), disclosure_missing 100% (n=28), tool_loop 93% (n=28), fee_mismatch 100% (n=28), user_frustrated 86% (n=28)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 94.3 |                 99.3 |           93.3 |              80.7 |       100   | **93.5** |                                  |          83   |         564 | 36%                    |
| rules      |                 81.7 |                 99.3 |           82.3 |              61.7 |       100   | 85.0     | +6.9 … +10.2                     |           0   |           2 | 0%                     |
| e5         |                 74   |                 86   |           53.3 |              72   |        89   | 74.9     | +16.5 … +20.8                    |           0   |         181 | 0%                     |
| jev        |                 88   |                 99.7 |           92.3 |              81.3 |       100   | 92.3     | +0.1 … +2.5                      |          36.5 |         198 | 0%                     |
| llm_ds     |                 97   |                 98.7 |           94   |              76.3 |        98   | 92.8     | -0.5 … +1.9                      |         135.2 |        1211 | 100%                   |
| llm_luna   |                 95.7 |                 99.7 |           93.3 |              83.7 |        93.3 | 93.1     | -1.0 … +1.9                      |          81.2 |        2064 | 100%                   |
| llm_haiku  |                 97   |                 99   |           94   |              83   |        91   | 92.8     | -0.4 … +1.9                      |         124.8 |         920 | 100%                   |

### hosted + 100 B labels: calibrated on 258 labelled conversations

Hosted evaluators chosen for the suite: llm_luna (dev: $80.8 / 1M conv, 2604 ms / conv, 1 question(s) below their floor).

| question           | chosen route          |   dev acc % |   best dev acc % | meets target   |
|--------------------|-----------------------|-------------|------------------|----------------|
| false_confirmation | e5 → llm_luna         |        98.1 |             98.8 | yes            |
| disclosure_missing | rules → e5 → llm_luna |        99.6 |            100   | yes            |
| fee_mismatch       | llm_luna              |       100   |            100   | yes            |
| user_frustrated    | e5 → llm_luna         |        90.3 |             90.3 | **no**         |
| tool_loop          | rules                 |       100   |            100   | yes            |

**B-rest** (200 conversations)

Drift monitor (agreement of routed decisions with the audit judge on the audited sample): disclosure_missing 100% (n=17), tool_loop 100% (n=17), false_confirmation 94% (n=17), fee_mismatch 100% (n=17), user_frustrated 88% (n=17)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 97.5 |                 99   |           99   |              87.5 |       100   | **96.6** |                                  |          77   |        1855 | 100%                   |
| rules      |                 82   |                 96   |           85   |              57.5 |       100   | 84.1     | +10.5 … +14.6                    |           0   |           3 | 0%                     |
| e5         |                 87   |                 88.5 |           59   |              77   |        91.5 | 80.6     | +13.8 … +18.3                    |           0   |         158 | 0%                     |
| jev        |                 87   |                100   |           94   |              89   |       100   | 94.0     | +1.2 … +4.0                      |          34.5 |         149 | 0%                     |
| llm_ds     |                 99   |                 99.5 |           98.5 |              84   |        99   | 96.0     | -0.9 … +2.2                      |         116.5 |        1188 | 100%                   |
| llm_luna   |                 97.5 |                100   |           99   |              87.5 |        91   | 95.0     | +0.6 … +2.6                      |          77   |        1768 | 100%                   |
| llm_haiku  |                 97.5 |                 99.5 |           98.5 |              85.5 |        91   | 94.4     | +0.9 … +3.5                      |         115.7 |         949 | 100%                   |

**C (Qwen; final holdout)** (300 conversations)

Drift monitor (agreement of routed decisions with the audit judge on the audited sample): disclosure_missing 96% (n=28), tool_loop 93% (n=28), user_frustrated 82% (n=28), false_confirmation 96% (n=28), fee_mismatch 100% (n=28)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 95   |                 95.7 |           93.3 |              83.7 |       100   | **93.5** |                                  |          81.2 |        2158 | 100%                   |
| rules      |                 81.7 |                 99.3 |           82.3 |              61.7 |       100   | 85.0     | +6.5 … +10.5                     |           0   |           3 | 0%                     |
| e5         |                 78.7 |                 93   |           57   |              73.3 |        84   | 77.2     | +14.3 … +18.4                    |           0   |         191 | 0%                     |
| jev        |                 88   |                 99.7 |           92.3 |              81.3 |       100   | 92.3     | +0.1 … +2.5                      |          36.5 |         198 | 0%                     |
| llm_ds     |                 97   |                 98.7 |           94   |              76.3 |        98   | 92.8     | -0.7 … +2.1                      |         135.2 |        1211 | 100%                   |
| llm_luna   |                 95.7 |                 99.7 |           93.3 |              83.7 |        93.3 | 93.1     | -0.6 … +1.4                      |          81.2 |        2064 | 100%                   |
| llm_haiku  |                 97   |                 99   |           94   |              83   |        91   | 92.8     | -0.7 … +2.1                      |         124.8 |         920 | 100%                   |

### local-only: calibrated on 158 labelled conversations

Hosted evaluators chosen for the suite: none (dev: $0.0 / 1M conv, 177 ms / conv, 0 question(s) below their floor).

| question           | chosen route   |   dev acc % |   best dev acc % | meets target   |
|--------------------|----------------|-------------|------------------|----------------|
| false_confirmation | rules → e5     |        93.7 |             93.7 | **no**         |
| disclosure_missing | rules          |       100   |            100   | yes            |
| fee_mismatch       | rules          |        83.5 |             83.5 | **no**         |
| user_frustrated    | e5             |        90.5 |             90.5 | **no**         |
| tool_loop          | rules          |       100   |            100   | yes            |

**A-test** (237 conversations)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 93.7 |                100   |           84   |              94.1 |       100   | **94.3** |                                  |             0 |          39 | 0%                     |
| rules      |                 77.2 |                100   |           84   |              62.9 |       100   | 84.8     | +7.8 … +11.2                     |             0 |           2 | 0%                     |
| e5         |                 80.2 |                 99.6 |           51.1 |              94.1 |        85.7 | 82.1     | +10.0 … +14.5                    |             0 |         184 | 0%                     |

**B (Gemini; used while iterating)** (300 conversations)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                   87 |                   96 |           85   |              76.7 |       100   | **88.9** |                                  |             0 |          46 | 0%                     |
| rules      |                   81 |                   96 |           85   |              57.7 |       100   | 83.9     | +3.3 … +6.5                      |             0 |           2 | 0%                     |
| e5         |                   83 |                   75 |           53.7 |              76.7 |        93.3 | 76.3     | +10.3 … +14.8                    |             0 |         148 | 0%                     |

**C (Qwen; final holdout)** (300 conversations)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 82.7 |                 99.3 |           82.3 |              72   |         100 | **87.3** |                                  |             0 |          49 | 0%                     |
| rules      |                 81.7 |                 99.3 |           82.3 |              61.7 |         100 | 85.0     | +0.5 … +4.1                      |             0 |           3 | 0%                     |
| e5         |                 74   |                 86   |           53.3 |              72   |          89 | 74.9     | +10.4 … +14.4                    |             0 |         178 | 0%                     |

### local-only + 100 B labels: calibrated on 258 labelled conversations

Hosted evaluators chosen for the suite: none (dev: $0.0 / 1M conv, 173 ms / conv, 0 question(s) below their floor).

| question           | chosen route   |   dev acc % |   best dev acc % | meets target   |
|--------------------|----------------|-------------|------------------|----------------|
| false_confirmation | rules → e5     |        89.9 |             89.9 | **no**         |
| disclosure_missing | rules          |        98.4 |             98.8 | yes            |
| fee_mismatch       | rules          |        84.1 |             84.1 | **no**         |
| user_frustrated    | e5             |        83.7 |             83.7 | **no**         |
| tool_loop          | rules          |       100   |            100   | yes            |

**B-rest** (200 conversations)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                   89 |                 96   |             85 |              77   |       100   | **89.4** |                                  |             0 |          43 | 0%                     |
| rules      |                   82 |                 96   |             85 |              57.5 |       100   | 84.1     | +3.3 … +7.2                      |             0 |           2 | 0%                     |
| e5         |                   87 |                 88.5 |             59 |              77   |        91.5 | 80.6     | +6.6 … +10.8                     |             0 |         142 | 0%                     |

**C (Qwen; final holdout)** (300 conversations)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 83.3 |                 99.3 |           82.3 |              73.3 |         100 | **87.7** |                                  |             0 |          42 | 0%                     |
| rules      |                 81.7 |                 99.3 |           82.3 |              61.7 |         100 | 85.0     | +0.9 … +4.4                      |             0 |           2 | 0%                     |
| e5         |                 78.7 |                 93   |           57   |              73.3 |          84 | 77.2     | +8.4 … +12.5                     |             0 |         178 | 0%                     |


Total OpenRouter spend so far: $1.55
