## Single layers (test split)

| question           | evaluator               |   n |   acc % |   F1 % |   kappa |   $ / 1k items |   ms / item |
|--------------------|-------------------------|-----|---------|--------|---------|----------------|-------------|
| false_confirmation | rules                   |  53 |    73.6 |   77.4 |    0.49 |         0      |           0 |
| false_confirmation | e5+logreg               |  53 |    54.7 |   53.8 |    0.1  |         0      |         430 |
| disclosure_missing | rules                   |  53 |   100   |  100   |    1    |         0      |           0 |
| disclosure_missing | e5+logreg               |  53 |    96.2 |   96   |    0.92 |         0      |          25 |
| fee_mismatch       | rules                   |  53 |    81.1 |   70.6 |    0.57 |         0      |           1 |
| fee_mismatch       | e5+logreg               |  53 |    52.8 |   28.6 |   -0.06 |         0      |          62 |
| user_frustrated    | rules                   |  53 |    60.4 |   58.8 |    0.21 |         0      |           2 |
| user_frustrated    | e5+logreg               |  53 |    77.4 |   71.4 |    0.53 |         0      |          23 |
| user_frustrated    | mDeBERTa zero-shot      |  53 |    43.4 |   60.5 |    0    |         0      |        1502 |
| tool_loop          | rules                   |  53 |   100   |  100   |    1    |         0      |           2 |
| tool_loop          | e5+logreg               |  53 |    83   |   80   |    0.66 |         0      |          72 |
| injection          | rules                   | 116 |    64.7 |   48.1 |    0.31 |         0      |           0 |
| injection          | Jev                     | 116 |    77.6 |   72.3 |    0.56 |         0.0142 |         183 |
| injection          | LLM DeepSeek-V4.1-Flash | 116 |    72.4 |   63.6 |    0.46 |         0.0097 |         807 |
| injection          | LLM GPT-4.1-nano        | 116 |    57.8 |   31   |    0.18 |         0.0133 |         773 |
| injection          | e5+logreg               | 116 |    93.1 |   93.3 |    0.86 |         0      |          95 |
| injection          | ProtectAI injection     | 116 |    67.2 |   53.7 |    0.36 |         0      |         949 |


## Layer combinations (test split)

Non-final layers stop the cascade only when confident; thresholds tuned on dev for ≥95% accuracy on confident items. 'decided by' = share of items each layer decided.

| question           | layers                                                |   n |   acc % |   F1 % |   kappa | decided by      |   $ / 1k |   ms / item |
|--------------------|-------------------------------------------------------|-----|---------|--------|---------|-----------------|----------|-------------|
| false_confirmation | e5+logreg                                             |  53 |    54.7 |   53.8 |    0.1  | 100%            |   0      |         430 |
| false_confirmation | rules → e5+logreg                                     |  53 |    84.9 |   85.2 |    0.7  | 68% / 32%       |   0      |         138 |
| false_confirmation | rules                                                 |  53 |    73.6 |   77.4 |    0.49 | 100%            |   0      |           0 |
| disclosure_missing | e5+logreg                                             |  53 |    96.2 |   96   |    0.92 | 100%            |   0      |          25 |
| disclosure_missing | rules → e5+logreg                                     |  53 |   100   |  100   |    1    | 100% / 0%       |   0      |           0 |
| disclosure_missing | rules                                                 |  53 |   100   |  100   |    1    | 100%            |   0      |           0 |
| fee_mismatch       | e5+logreg                                             |  53 |    52.8 |   28.6 |   -0.06 | 100%            |   0      |          62 |
| fee_mismatch       | rules → e5+logreg                                     |  53 |    52.8 |   28.6 |   -0.06 | 0% / 100%       |   0      |          63 |
| fee_mismatch       | rules                                                 |  53 |    81.1 |   70.6 |    0.57 | 100%            |   0      |           1 |
| user_frustrated    | e5+logreg                                             |  53 |    77.4 |   71.4 |    0.53 | 100%            |   0      |          23 |
| user_frustrated    | rules → e5+logreg                                     |  53 |    79.2 |   74.4 |    0.57 | 4% / 96%        |   0      |          24 |
| user_frustrated    | rules                                                 |  53 |    60.4 |   58.8 |    0.21 | 100%            |   0      |           2 |
| tool_loop          | e5+logreg                                             |  53 |    83   |   80   |    0.66 | 100%            |   0      |          72 |
| tool_loop          | rules → e5+logreg                                     |  53 |   100   |  100   |    1    | 100% / 0%       |   0      |           2 |
| tool_loop          | rules                                                 |  53 |   100   |  100   |    1    | 100%            |   0      |           2 |
| injection          | Jev                                                   | 116 |    77.6 |   72.3 |    0.56 | 100%            |   0.0142 |         183 |
| injection          | Jev → LLM DeepSeek-V4.1-Flash                         | 116 |    82.8 |   80   |    0.66 | 83% / 17%       |   0.0163 |         370 |
| injection          | rules → Jev                                           | 116 |    80.2 |   76.3 |    0.61 | 16% / 84%       |   0.0118 |         159 |
| injection          | rules → Jev → LLM DeepSeek-V4.1-Flash                 | 116 |    82.8 |   80   |    0.66 | 16% / 69% / 15% |   0.0135 |         312 |
| injection          | e5+logreg                                             | 116 |    93.1 |   93.3 |    0.86 | 100%            |   0      |          95 |
| injection          | e5+logreg → LLM DeepSeek-V4.1-Flash                   | 116 |    93.1 |   93.3 |    0.86 | 100% / 0%       |   0      |          95 |
| injection          | rules → e5+logreg                                     | 116 |    93.1 |   93.3 |    0.86 | 16% / 84%       |   0      |          80 |
| injection          | rules → e5+logreg → LLM DeepSeek-V4.1-Flash           | 116 |    93.1 |   93.3 |    0.86 | 16% / 84% / 0%  |   0      |          80 |
| injection          | ProtectAI injection                                   | 116 |    67.2 |   53.7 |    0.36 | 100%            |   0      |         949 |
| injection          | ProtectAI injection → LLM DeepSeek-V4.1-Flash         | 116 |    78.4 |   73.7 |    0.57 | 19% / 81%       |   0.0077 |        1580 |
| injection          | rules → ProtectAI injection                           | 116 |    75   |   68.1 |    0.51 | 16% / 84%       |   0      |         793 |
| injection          | rules → ProtectAI injection → LLM DeepSeek-V4.1-Flash | 116 |    79.3 |   75   |    0.59 | 16% / 10% / 73% |   0.0069 |        1350 |
| injection          | LLM DeepSeek-V4.1-Flash                               | 116 |    72.4 |   63.6 |    0.46 | 100%            |   0.0097 |         807 |
| injection          | rules                                                 | 116 |    64.7 |   48.1 |    0.31 | 100%            |   0      |           0 |
| injection          | rules → LLM DeepSeek-V4.1-Flash                       | 116 |    75   |   68.1 |    0.51 | 16% / 84%       |   0.0077 |         666 |


## Spanish vs English accuracy (test split)

| question           | evaluator   |   es % |   en % |   es − en |
|--------------------|-------------|--------|--------|-----------|
| false_confirmation | rules       |   65.4 |   81.5 |     -16.1 |
| false_confirmation | e5+logreg   |   57.7 |   51.9 |       5.8 |
| disclosure_missing | rules       |  100   |  100   |       0   |
| disclosure_missing | e5+logreg   |  100   |   92.6 |       7.4 |
| fee_mismatch       | rules       |   84.6 |   77.8 |       6.8 |
| fee_mismatch       | e5+logreg   |   42.3 |   63   |     -20.7 |
| user_frustrated    | rules       |   53.8 |   66.7 |     -12.8 |
| user_frustrated    | e5+logreg   |   76.9 |   77.8 |      -0.9 |
| tool_loop          | rules       |  100   |  100   |       0   |
| tool_loop          | e5+logreg   |   80.8 |   85.2 |      -4.4 |


## Agreement between the two LLM judges (Cohen's kappa)

| question   |   kappa |
|------------|---------|
| injection  |    0.44 |
