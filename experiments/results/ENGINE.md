# Engine evaluation

Suite: `banking_suite.yaml`. Targets and tolerance from the suite policy.

### hosted: calibrated on 158 labelled conversations

| question           | chosen route   |   dev acc % |   best dev acc % | meets target   |
|--------------------|----------------|-------------|------------------|----------------|
| false_confirmation | llm_haiku      |        99.4 |             99.4 | yes            |
| disclosure_missing | rules          |       100   |            100   | yes            |
| fee_mismatch       | llm_luna       |       100   |            100   | yes            |
| user_frustrated    | e5 → jev       |        93   |             93   | **no**         |
| tool_loop          | rules          |       100   |            100   | yes            |

**A-test** (237 conversations)

Drift monitor (agreement of routed decisions with the audit judge on the audited sample): disclosure_missing 100% (n=17), tool_loop 94% (n=17), user_frustrated 82% (n=17), fee_mismatch 100% (n=17), false_confirmation 94% (n=17)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 99.2 |                100   |          100   |              91.6 |       100   | **98.1** |                                  |         212.3 |        3419 | 100%                   |
| rules      |                 77.2 |                100   |           84   |              62.9 |       100   | 84.8     | +11.3 … +15.3                    |           0   |           2 | 0%                     |
| e5         |                 80.2 |                 99.6 |           51.1 |              94.1 |        85.7 | 82.1     | +14.1 … +18.2                    |           0   |         319 | 0%                     |
| jev        |                 87.3 |                100   |          100   |              91.1 |       100   | 95.7     | +1.4 … +3.5                      |          37.6 |         190 | 0%                     |
| llm_ds     |                 98.7 |                100   |           99.2 |              87.3 |        98.7 | 96.8     | +0.3 … +2.4                      |          74.7 |        1200 | 100%                   |
| llm_luna   |                 99.6 |                100   |          100   |              90.7 |        87.3 | 95.5     | +1.6 … +3.7                      |          83.2 |        2419 | 100%                   |
| llm_haiku  |                 99.2 |                 99.6 |          100   |              87.8 |        94.9 | 96.3     | +0.9 … +2.8                      |         124.5 |         888 | 100%                   |

**B (unseen generator)** (300 conversations)

Drift monitor (agreement of routed decisions with the audit judge on the audited sample): disclosure_missing 81% (n=27), tool_loop 100% (n=27), user_frustrated 81% (n=27), fee_mismatch 100% (n=27), false_confirmation 96% (n=27)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 97.3 |                 80.3 |           99.3 |              81.7 |       100   | **91.7** |                                  |         204.5 |        2937 | 100%                   |
| rules      |                 81   |                 80.3 |           85   |              57.7 |       100   | 80.8     | +9.1 … +12.7                     |           0   |           2 | 0%                     |
| e5         |                 83   |                 66.7 |           53.7 |              76.7 |        93.3 | 74.7     | +15.1 … +19.1                    |           0   |         136 | 0%                     |
| jev        |                 86.3 |                100   |           94.7 |              85.7 |       100   | 93.3     | -3.1 … +0.0                      |          34.4 |         175 | 0%                     |
| llm_ds     |                 98.7 |                 99.7 |           99   |              80.7 |        98.3 | 95.3     | -5.0 … -1.9                      |         112.5 |        1172 | 100%                   |
| llm_luna   |                 97   |                 99.7 |           99.3 |              86   |        92   | 94.8     | -4.6 … -1.5                      |          76.8 |        1898 | 100%                   |
| llm_haiku  |                 97.3 |                 99.7 |           98.3 |              83   |        92.3 | 94.1     | -3.9 … -0.8                      |         115.7 |         948 | 100%                   |

### hosted + 100 B labels: calibrated on 258 labelled conversations

| question           | chosen route   |   dev acc % |   best dev acc % | meets target   |
|--------------------|----------------|-------------|------------------|----------------|
| false_confirmation | llm_ds         |        98.4 |             98.4 | yes            |
| disclosure_missing | rules → jev    |       100   |            100   | yes            |
| fee_mismatch       | llm_luna       |       100   |            100   | yes            |
| user_frustrated    | e5 → llm_luna  |        90.3 |             90.3 | **no**         |
| tool_loop          | rules          |       100   |            100   | yes            |

**B-rest** (200 conversations)

Drift monitor (agreement of routed decisions with the audit judge on the audited sample): disclosure_missing 100% (n=17), tool_loop 100% (n=17), false_confirmation 100% (n=17), fee_mismatch 100% (n=17), user_frustrated 88% (n=17)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                 99   |                100   |           99   |              87.5 |       100   | **97.1** |                                  |         215.8 |        3079 | 100%                   |
| rules      |                 82   |                 79.5 |           85   |              57.5 |       100   | 80.8     | +14.1 … +18.4                    |           0   |           2 | 0%                     |
| e5         |                 87   |                 77   |           59   |              77   |        91.5 | 78.3     | +16.5 … +21.1                    |           0   |         156 | 0%                     |
| jev        |                 87   |                100   |           94   |              89   |       100   | 94.0     | +1.7 … +4.6                      |          34.5 |         149 | 0%                     |
| llm_ds     |                 99   |                 99.5 |           98.5 |              84   |        99   | 96.0     | -0.2 … +2.6                      |         116.5 |        1188 | 100%                   |
| llm_luna   |                 97.5 |                100   |           99   |              87.5 |        91   | 95.0     | +1.1 … +3.1                      |          77   |        1768 | 100%                   |
| llm_haiku  |                 97.5 |                 99.5 |           98.5 |              85.5 |        91   | 94.4     | +1.5 … +4.0                      |         115.7 |         949 | 100%                   |

### local-only: calibrated on 158 labelled conversations

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
| **engine** |                 93.7 |                100   |           84   |              94.1 |       100   | **94.3** |                                  |             0 |          46 | 0%                     |
| rules      |                 77.2 |                100   |           84   |              62.9 |       100   | 84.8     | +7.8 … +11.2                     |             0 |           2 | 0%                     |
| e5         |                 80.2 |                 99.6 |           51.1 |              94.1 |        85.7 | 82.1     | +10.0 … +14.4                    |             0 |         412 | 0%                     |

**B (unseen generator)** (300 conversations)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                   87 |                 80.3 |           85   |              76.7 |       100   | **85.8** |                                  |             0 |         121 | 0%                     |
| rules      |                   81 |                 80.3 |           85   |              57.7 |       100   | 80.8     | +3.3 … +6.5                      |             0 |           2 | 0%                     |
| e5         |                   83 |                 66.7 |           53.7 |              76.7 |        93.3 | 74.7     | +9.1 … +13.2                     |             0 |         134 | 0%                     |

### local-only + 100 B labels: calibrated on 258 labelled conversations

| question           | chosen route   |   dev acc % |   best dev acc % | meets target   |
|--------------------|----------------|-------------|------------------|----------------|
| false_confirmation | rules → e5     |        89.9 |             89.9 | **no**         |
| disclosure_missing | rules → e5     |        94.2 |             94.2 | **no**         |
| fee_mismatch       | rules          |        84.1 |             84.1 | **no**         |
| user_frustrated    | e5             |        83.7 |             83.7 | **no**         |
| tool_loop          | rules          |       100   |            100   | yes            |

**B-rest** (200 conversations)

|            |   false_confirmation |   disclosure_missing |   fee_mismatch |   user_frustrated |   tool_loop | mean %   | engine − this (95% CI, points)   |   $ / 1M conv |   ms / conv | conv. sent to an LLM   |
|------------|----------------------|----------------------|----------------|-------------------|-------------|----------|----------------------------------|---------------|-------------|------------------------|
| **engine** |                   89 |                 80.5 |             85 |              77   |       100   | **86.3** |                                  |             0 |         289 | 0%                     |
| rules      |                   82 |                 79.5 |             85 |              57.5 |       100   | 80.8     | +3.6 … +7.4                      |             0 |           2 | 0%                     |
| e5         |                   87 |                 77   |             59 |              77   |        91.5 | 78.3     | +5.8 … +10.1                     |             0 |         463 | 0%                     |


Total OpenRouter spend so far: $1.22
