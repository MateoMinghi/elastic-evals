## Single layers (test split)

| question           | evaluator                    |   n |   acc % |   F1 % |   kappa |   $ / 1k items |   ms / item |
|--------------------|------------------------------|-----|---------|--------|---------|----------------|-------------|
| false_confirmation | rules                        | 237 |    77.2 |   78.4 |    0.57 |         0      |           0 |
| false_confirmation | Jev                          | 237 |    87.3 |   86.7 |    0.75 |         0.0075 |         190 |
| false_confirmation | LLM DeepSeek-V4.1-Flash      | 237 |    98.7 |   98.5 |    0.97 |         0.0149 |        1200 |
| false_confirmation | LLM GPT-4.1-nano             | 237 |    71.7 |   55.6 |    0.37 |         0.0161 |         978 |
| false_confirmation | LLM GPT-6 Luna               | 237 |    99.6 |   99.5 |    0.99 |         0.0166 |        2419 |
| false_confirmation | LLM Claude Haiku 5.5         | 237 |    99.2 |   99   |    0.98 |         0.0249 |         888 |
| false_confirmation | e5+logreg                    | 237 |    80.2 |   75.9 |    0.59 |         0      |         128 |
| disclosure_missing | rules                        | 237 |   100   |  100   |    1    |         0      |           0 |
| disclosure_missing | Jev                          | 237 |   100   |  100   |    1    |         0.0075 |         190 |
| disclosure_missing | LLM DeepSeek-V4.1-Flash      | 237 |   100   |  100   |    1    |         0.0149 |        1200 |
| disclosure_missing | LLM GPT-4.1-nano             | 237 |    97   |   96.6 |    0.94 |         0.0161 |         978 |
| disclosure_missing | LLM GPT-6 Luna               | 237 |   100   |  100   |    1    |         0.0166 |        2419 |
| disclosure_missing | LLM Claude Haiku 5.5         | 237 |    99.6 |   99.5 |    0.99 |         0.0249 |         888 |
| disclosure_missing | e5+logreg                    | 237 |    99.6 |   99.5 |    0.99 |         0      |          21 |
| fee_mismatch       | rules                        | 237 |    84   |   77.9 |    0.65 |         0      |           1 |
| fee_mismatch       | Jev                          | 237 |   100   |  100   |    1    |         0.0075 |         190 |
| fee_mismatch       | LLM DeepSeek-V4.1-Flash      | 237 |    99.2 |   98.8 |    0.98 |         0.0149 |        1200 |
| fee_mismatch       | LLM GPT-4.1-nano             | 237 |    76.8 |   49.5 |    0.39 |         0.0161 |         978 |
| fee_mismatch       | LLM GPT-6 Luna               | 237 |   100   |  100   |    1    |         0.0166 |        2419 |
| fee_mismatch       | LLM Claude Haiku 5.5         | 237 |   100   |  100   |    1    |         0.0249 |         888 |
| fee_mismatch       | e5+logreg                    | 237 |    51.1 |   37   |   -0.02 |         0      |         121 |
| user_frustrated    | rules                        | 237 |    62.9 |   64.5 |    0.29 |         0      |           2 |
| user_frustrated    | Jev                          | 237 |    91.1 |   89.4 |    0.82 |         0.0075 |         190 |
| user_frustrated    | LLM DeepSeek-V4.1-Flash      | 237 |    87.3 |   85.3 |    0.74 |         0.0149 |        1200 |
| user_frustrated    | LLM GPT-4.1-nano             | 237 |    85.2 |   82.1 |    0.7  |         0.0161 |         978 |
| user_frustrated    | LLM GPT-6 Luna               | 237 |    90.7 |   88.4 |    0.81 |         0.0166 |        2419 |
| user_frustrated    | LLM Claude Haiku 5.5         | 237 |    87.8 |   85.6 |    0.75 |         0.0249 |         888 |
| user_frustrated    | e5+logreg                    | 237 |    94.1 |   92.7 |    0.88 |         0      |          79 |
| tool_loop          | rules                        | 237 |   100   |  100   |    1    |         0      |           2 |
| tool_loop          | Jev                          | 237 |   100   |  100   |    1    |         0.0075 |         190 |
| tool_loop          | LLM DeepSeek-V4.1-Flash      | 237 |    98.7 |   97.8 |    0.97 |         0.0149 |        1200 |
| tool_loop          | LLM GPT-4.1-nano             | 237 |    87.3 |   77.6 |    0.69 |         0.0161 |         978 |
| tool_loop          | LLM GPT-6 Luna               | 237 |    87.3 |   73.2 |    0.66 |         0.0166 |        2419 |
| tool_loop          | LLM Claude Haiku 5.5         | 237 |    94.9 |   90.8 |    0.87 |         0.0249 |         888 |
| tool_loop          | e5+logreg                    | 237 |    85.7 |   80.2 |    0.7  |         0      |         157 |
| massive            | Jev                          | 600 |    87.5 |   86.7 |    0.86 |         0.0273 |         182 |
| massive            | LLM DeepSeek-V4.1-Flash      | 600 |    85.3 |   85.4 |    0.84 |         0.0231 |        1111 |
| massive            | LLM GPT-4.1-nano             | 600 |    73.7 |   76.9 |    0.72 |         0.0287 |         739 |
| massive            | LLM GPT-6 Luna               | 600 |    84   |   83.6 |    0.83 |         0.0318 |        1733 |
| massive            | LLM Claude Haiku 5.5         | 600 |    85.7 |   84.7 |    0.85 |         0.0465 |         872 |
| massive            | e5+logreg                    | 600 |    72.8 |   71.6 |    0.71 |         0      |          12 |
| massive            | e5+logreg (600 human labels) | 600 |    81.3 |   81   |    0.8  |         0      |          12 |
| massive            | e5+logreg distilled from LLM | 600 |    79.7 |   79.1 |    0.78 |         0      |          19 |
| sentiment          | Jev                          | 600 |    71.8 |   69.4 |    0.58 |         0.0156 |         183 |
| sentiment          | LLM DeepSeek-V4.1-Flash      | 600 |    73   |   72.3 |    0.6  |         0.011  |         660 |
| sentiment          | LLM GPT-4.1-nano             | 600 |    69   |   67.5 |    0.54 |         0.0122 |         759 |
| sentiment          | LLM GPT-6 Luna               | 600 |    73   |   72.3 |    0.59 |         0.0153 |        1816 |
| sentiment          | LLM Claude Haiku 5.5         | 600 |    72.3 |   72   |    0.58 |         0.0204 |         863 |
| sentiment          | e5+logreg                    | 600 |    61.3 |   60.6 |    0.42 |         0      |           8 |
| sentiment          | e5+logreg (600 human labels) | 600 |    64   |   62.8 |    0.46 |         0      |           8 |
| sentiment          | e5+logreg distilled from LLM | 600 |    65.7 |   65.3 |    0.48 |         0      |           9 |
| injection          | rules                        | 116 |    64.7 |   48.1 |    0.31 |         0      |           0 |
| injection          | Jev                          | 116 |    77.6 |   72.3 |    0.56 |         0.0142 |         183 |
| injection          | LLM DeepSeek-V4.1-Flash      | 116 |    72.4 |   63.6 |    0.46 |         0.0097 |         807 |
| injection          | LLM GPT-4.1-nano             | 116 |    57.8 |   31   |    0.18 |         0.0133 |         773 |
| injection          | LLM GPT-6 Luna               | 116 |    82.8 |   80   |    0.66 |         0.0169 |        1523 |
| injection          | LLM Claude Haiku 5.5         | 116 |    78.4 |   73.7 |    0.57 |         0.0192 |         858 |
| injection          | e5+logreg                    | 116 |    93.1 |   93.3 |    0.86 |         0      |          19 |
| injection          | ProtectAI injection          | 116 |    67.2 |   53.7 |    0.36 |         0      |         187 |


## Layer combinations (test split)

Non-final layers stop the cascade only when confident; thresholds tuned on dev for ≥95% accuracy on confident items. 'decided by' = share of items each layer decided.

| question           | layers                                                 |   n |   acc % |   F1 % |   kappa | decided by      |   $ / 1k |   ms / item |
|--------------------|--------------------------------------------------------|-----|---------|--------|---------|-----------------|----------|-------------|
| false_confirmation | Jev                                                    | 237 |    87.3 |   86.7 |    0.75 | 100%            |   0.0075 |         190 |
| false_confirmation | Jev → LLM DeepSeek-V4.1-Flash                          | 237 |    94.1 |   93.3 |    0.88 | 93% / 7%        |   0.0081 |         293 |
| false_confirmation | rules → Jev                                            | 237 |    88.2 |   87.5 |    0.77 | 73% / 27%       |   0.002  |          44 |
| false_confirmation | rules → Jev → LLM DeepSeek-V4.1-Flash                  | 237 |    94.1 |   93.3 |    0.88 | 73% / 21% / 6%  |   0.0026 |         125 |
| false_confirmation | e5+logreg                                              | 237 |    80.2 |   75.9 |    0.59 | 100%            |   0      |         128 |
| false_confirmation | e5+logreg → LLM DeepSeek-V4.1-Flash                    | 237 |    96.6 |   95.7 |    0.93 | 53% / 47%       |   0.0072 |         707 |
| false_confirmation | rules → e5+logreg                                      | 237 |    93.7 |   92.8 |    0.87 | 73% / 27%       |   0      |          34 |
| false_confirmation | rules → e5+logreg → LLM DeepSeek-V4.1-Flash            | 237 |    99.2 |   99   |    0.98 | 73% / 15% / 12% |   0.0018 |         177 |
| false_confirmation | LLM DeepSeek-V4.1-Flash                                | 237 |    98.7 |   98.5 |    0.97 | 100%            |   0.0149 |        1200 |
| false_confirmation | rules                                                  | 237 |    77.2 |   78.4 |    0.57 | 100%            |   0      |           0 |
| false_confirmation | rules → LLM DeepSeek-V4.1-Flash                        | 237 |    98.7 |   98.5 |    0.97 | 73% / 27%       |   0.0041 |         323 |
| disclosure_missing | Jev                                                    | 237 |   100   |  100   |    1    | 100%            |   0.0075 |         190 |
| disclosure_missing | Jev → LLM DeepSeek-V4.1-Flash                          | 237 |   100   |  100   |    1    | 100% / 0%       |   0.0075 |         190 |
| disclosure_missing | rules → Jev                                            | 237 |   100   |  100   |    1    | 100% / 0%       |   0      |           0 |
| disclosure_missing | rules → Jev → LLM DeepSeek-V4.1-Flash                  | 237 |   100   |  100   |    1    | 100% / 0% / 0%  |   0      |           0 |
| disclosure_missing | e5+logreg                                              | 237 |    99.6 |   99.5 |    0.99 | 100%            |   0      |          21 |
| disclosure_missing | e5+logreg → LLM DeepSeek-V4.1-Flash                    | 237 |   100   |  100   |    1    | 99% / 1%        |   0.0002 |          25 |
| disclosure_missing | rules → e5+logreg                                      | 237 |   100   |  100   |    1    | 100% / 0%       |   0      |           0 |
| disclosure_missing | rules → e5+logreg → LLM DeepSeek-V4.1-Flash            | 237 |   100   |  100   |    1    | 100% / 0% / 0%  |   0      |           0 |
| disclosure_missing | LLM DeepSeek-V4.1-Flash                                | 237 |   100   |  100   |    1    | 100%            |   0.0149 |        1200 |
| disclosure_missing | rules                                                  | 237 |   100   |  100   |    1    | 100%            |   0      |           0 |
| disclosure_missing | rules → LLM DeepSeek-V4.1-Flash                        | 237 |   100   |  100   |    1    | 100% / 0%       |   0      |           0 |
| fee_mismatch       | Jev                                                    | 237 |   100   |  100   |    1    | 100%            |   0.0075 |         190 |
| fee_mismatch       | Jev → LLM DeepSeek-V4.1-Flash                          | 237 |   100   |  100   |    1    | 100% / 0%       |   0.0075 |         190 |
| fee_mismatch       | rules → Jev                                            | 237 |   100   |  100   |    1    | 0% / 100%       |   0.0075 |         191 |
| fee_mismatch       | rules → Jev → LLM DeepSeek-V4.1-Flash                  | 237 |   100   |  100   |    1    | 0% / 100% / 0%  |   0.0075 |         191 |
| fee_mismatch       | e5+logreg                                              | 237 |    51.1 |   37   |   -0.02 | 100%            |   0      |         121 |
| fee_mismatch       | e5+logreg → LLM DeepSeek-V4.1-Flash                    | 237 |    99.2 |   98.8 |    0.98 | 0% / 100%       |   0.0149 |        1321 |
| fee_mismatch       | rules → e5+logreg                                      | 237 |    51.1 |   37   |   -0.02 | 0% / 100%       |   0      |         122 |
| fee_mismatch       | rules → e5+logreg → LLM DeepSeek-V4.1-Flash            | 237 |    99.2 |   98.8 |    0.98 | 0% / 0% / 100%  |   0.0149 |        1322 |
| fee_mismatch       | LLM DeepSeek-V4.1-Flash                                | 237 |    99.2 |   98.8 |    0.98 | 100%            |   0.0149 |        1200 |
| fee_mismatch       | rules                                                  | 237 |    84   |   77.9 |    0.65 | 100%            |   0      |           1 |
| fee_mismatch       | rules → LLM DeepSeek-V4.1-Flash                        | 237 |    99.2 |   98.8 |    0.98 | 0% / 100%       |   0.0149 |        1201 |
| user_frustrated    | Jev                                                    | 237 |    91.1 |   89.4 |    0.82 | 100%            |   0.0075 |         190 |
| user_frustrated    | Jev → LLM DeepSeek-V4.1-Flash                          | 237 |    89   |   87.6 |    0.78 | 89% / 11%       |   0.009  |         332 |
| user_frustrated    | rules → Jev                                            | 237 |    91.1 |   89.4 |    0.82 | 0% / 100%       |   0.0075 |         193 |
| user_frustrated    | rules → Jev → LLM DeepSeek-V4.1-Flash                  | 237 |    89   |   87.6 |    0.78 | 0% / 89% / 11%  |   0.009  |         334 |
| user_frustrated    | e5+logreg                                              | 237 |    94.1 |   92.7 |    0.88 | 100%            |   0      |          79 |
| user_frustrated    | e5+logreg → LLM DeepSeek-V4.1-Flash                    | 237 |    92   |   90.4 |    0.83 | 88% / 12%       |   0.0014 |         197 |
| user_frustrated    | rules → e5+logreg                                      | 237 |    94.1 |   92.7 |    0.88 | 0% / 100%       |   0      |          82 |
| user_frustrated    | rules → e5+logreg → LLM DeepSeek-V4.1-Flash            | 237 |    92   |   90.4 |    0.83 | 0% / 88% / 12%  |   0.0014 |         199 |
| user_frustrated    | LLM DeepSeek-V4.1-Flash                                | 237 |    87.3 |   85.3 |    0.74 | 100%            |   0.0149 |        1200 |
| user_frustrated    | rules                                                  | 237 |    62.9 |   64.5 |    0.29 | 100%            |   0      |           2 |
| user_frustrated    | rules → LLM DeepSeek-V4.1-Flash                        | 237 |    87.3 |   85.3 |    0.74 | 0% / 100%       |   0.0149 |        1202 |
| tool_loop          | Jev                                                    | 237 |   100   |  100   |    1    | 100%            |   0.0075 |         190 |
| tool_loop          | Jev → LLM DeepSeek-V4.1-Flash                          | 237 |   100   |  100   |    1    | 100% / 0%       |   0.0077 |         192 |
| tool_loop          | rules → Jev                                            | 237 |   100   |  100   |    1    | 100% / 0%       |   0      |           2 |
| tool_loop          | rules → Jev → LLM DeepSeek-V4.1-Flash                  | 237 |   100   |  100   |    1    | 100% / 0% / 0%  |   0      |           2 |
| tool_loop          | e5+logreg                                              | 237 |    85.7 |   80.2 |    0.7  | 100%            |   0      |         157 |
| tool_loop          | e5+logreg → LLM DeepSeek-V4.1-Flash                    | 237 |    94.9 |   92   |    0.88 | 91% / 9%        |   0.0011 |         314 |
| tool_loop          | rules → e5+logreg                                      | 237 |   100   |  100   |    1    | 100% / 0%       |   0      |           2 |
| tool_loop          | rules → e5+logreg → LLM DeepSeek-V4.1-Flash            | 237 |   100   |  100   |    1    | 100% / 0% / 0%  |   0      |           2 |
| tool_loop          | LLM DeepSeek-V4.1-Flash                                | 237 |    98.7 |   97.8 |    0.97 | 100%            |   0.0149 |        1200 |
| tool_loop          | rules                                                  | 237 |   100   |  100   |    1    | 100%            |   0      |           2 |
| tool_loop          | rules → LLM DeepSeek-V4.1-Flash                        | 237 |   100   |  100   |    1    | 100% / 0%       |   0      |           2 |
| massive            | Jev                                                    | 600 |    87.5 |   86.7 |    0.86 | 100%            |   0.0273 |         182 |
| massive            | Jev → LLM DeepSeek-V4.1-Flash                          | 600 |    87   |   86.9 |    0.86 | 72% / 28%       |   0.0337 |         485 |
| massive            | e5+logreg                                              | 600 |    72.8 |   71.6 |    0.71 | 100%            |   0      |          12 |
| massive            | e5+logreg → LLM DeepSeek-V4.1-Flash                    | 600 |    85.3 |   85.4 |    0.84 | 0% / 100%       |   0.0231 |        1123 |
| massive            | e5+logreg distilled from LLM                           | 600 |    79.7 |   79.1 |    0.78 | 100%            |   0      |          19 |
| massive            | e5+logreg distilled from LLM → LLM DeepSeek-V4.1-Flash | 600 |    85.8 |   85.8 |    0.85 | 52% / 48%       |   0.0113 |         531 |
| massive            | LLM DeepSeek-V4.1-Flash                                | 600 |    85.3 |   85.4 |    0.84 | 100%            |   0.0231 |        1111 |
| sentiment          | Jev                                                    | 600 |    71.8 |   69.4 |    0.58 | 100%            |   0.0156 |         183 |
| sentiment          | Jev → LLM DeepSeek-V4.1-Flash                          | 600 |    73   |   72.3 |    0.6  | 26% / 74%       |   0.0237 |         685 |
| sentiment          | e5+logreg                                              | 600 |    61.3 |   60.6 |    0.42 | 100%            |   0      |           8 |
| sentiment          | e5+logreg → LLM DeepSeek-V4.1-Flash                    | 600 |    73.3 |   72.5 |    0.6  | 9% / 91%        |   0.01   |         614 |
| sentiment          | e5+logreg distilled from LLM                           | 600 |    65.7 |   65.3 |    0.48 | 100%            |   0      |           9 |
| sentiment          | e5+logreg distilled from LLM → LLM DeepSeek-V4.1-Flash | 600 |    73.5 |   72.7 |    0.6  | 5% / 95%        |   0.0105 |         641 |
| sentiment          | LLM DeepSeek-V4.1-Flash                                | 600 |    73   |   72.3 |    0.6  | 100%            |   0.011  |         660 |
| injection          | Jev                                                    | 116 |    77.6 |   72.3 |    0.56 | 100%            |   0.0142 |         183 |
| injection          | Jev → LLM DeepSeek-V4.1-Flash                          | 116 |    82.8 |   80   |    0.66 | 83% / 17%       |   0.0163 |         370 |
| injection          | rules → Jev                                            | 116 |    80.2 |   76.3 |    0.61 | 16% / 84%       |   0.0118 |         159 |
| injection          | rules → Jev → LLM DeepSeek-V4.1-Flash                  | 116 |    82.8 |   80   |    0.66 | 16% / 69% / 15% |   0.0135 |         312 |
| injection          | e5+logreg                                              | 116 |    93.1 |   93.3 |    0.86 | 100%            |   0      |          19 |
| injection          | e5+logreg → LLM DeepSeek-V4.1-Flash                    | 116 |    85.3 |   83.8 |    0.71 | 75% / 25%       |   0.0026 |         185 |
| injection          | rules → e5+logreg                                      | 116 |    93.1 |   93.3 |    0.86 | 16% / 84%       |   0      |          16 |
| injection          | rules → e5+logreg → LLM DeepSeek-V4.1-Flash            | 116 |    86.2 |   84.9 |    0.73 | 16% / 60% / 23% |   0.0025 |         173 |
| injection          | ProtectAI injection                                    | 116 |    67.2 |   53.7 |    0.36 | 100%            |   0      |         187 |
| injection          | ProtectAI injection → LLM DeepSeek-V4.1-Flash          | 116 |    78.4 |   73.7 |    0.57 | 19% / 81%       |   0.0077 |         818 |
| injection          | rules → ProtectAI injection                            | 116 |    75   |   68.1 |    0.51 | 16% / 84%       |   0      |         156 |
| injection          | rules → ProtectAI injection → LLM DeepSeek-V4.1-Flash  | 116 |    79.3 |   75   |    0.59 | 16% / 10% / 73% |   0.0069 |         713 |
| injection          | LLM DeepSeek-V4.1-Flash                                | 116 |    72.4 |   63.6 |    0.46 | 100%            |   0.0097 |         807 |
| injection          | rules                                                  | 116 |    64.7 |   48.1 |    0.31 | 100%            |   0      |           0 |
| injection          | rules → LLM DeepSeek-V4.1-Flash                        | 116 |    75   |   68.1 |    0.51 | 16% / 84%       |   0.0077 |         666 |


## Spanish vs English accuracy (test split)

| question           | evaluator               |   es % |   en % |   es − en |
|--------------------|-------------------------|--------|--------|-----------|
| false_confirmation | rules                   |   75.2 |   79.2 |      -4   |
| false_confirmation | Jev                     |   88.9 |   85.8 |       3.1 |
| false_confirmation | e5+logreg               |   78.6 |   81.7 |      -3   |
| false_confirmation | LLM DeepSeek-V4.1-Flash |   99.1 |   98.3 |       0.8 |
| disclosure_missing | rules                   |  100   |  100   |       0   |
| disclosure_missing | Jev                     |  100   |  100   |       0   |
| disclosure_missing | e5+logreg               |   99.1 |  100   |      -0.9 |
| disclosure_missing | LLM DeepSeek-V4.1-Flash |  100   |  100   |       0   |
| fee_mismatch       | rules                   |   89.7 |   78.3 |      11.4 |
| fee_mismatch       | Jev                     |  100   |  100   |       0   |
| fee_mismatch       | e5+logreg               |   45.3 |   56.7 |     -11.4 |
| fee_mismatch       | LLM DeepSeek-V4.1-Flash |   99.1 |   99.2 |      -0   |
| user_frustrated    | rules                   |   53   |   72.5 |     -19.5 |
| user_frustrated    | Jev                     |   92.3 |   90   |       2.3 |
| user_frustrated    | e5+logreg               |   92.3 |   95.8 |      -3.5 |
| user_frustrated    | LLM DeepSeek-V4.1-Flash |   84.6 |   90   |      -5.4 |
| tool_loop          | rules                   |  100   |  100   |       0   |
| tool_loop          | Jev                     |  100   |  100   |       0   |
| tool_loop          | e5+logreg               |   84.6 |   86.7 |      -2.1 |
| tool_loop          | LLM DeepSeek-V4.1-Flash |  100   |   97.5 |       2.5 |
| massive            | Jev                     |   87.7 |   87.3 |       0.3 |
| massive            | e5+logreg               |   70.3 |   75.3 |      -5   |
| massive            | LLM DeepSeek-V4.1-Flash |   83   |   87.7 |      -4.7 |
| sentiment          | Jev                     |   71.3 |   72.3 |      -1   |
| sentiment          | e5+logreg               |   56.3 |   66.3 |     -10   |
| sentiment          | LLM DeepSeek-V4.1-Flash |   71.3 |   74.7 |      -3.3 |


## Agreement between the two LLM judges (Cohen's kappa)

| question           |   kappa |
|--------------------|---------|
| false_confirmation |    0.37 |
| disclosure_missing |    0.94 |
| fee_mismatch       |    0.4  |
| user_frustrated    |    0.84 |
| tool_loop          |    0.65 |
| massive            |    0.79 |
| sentiment          |    0.67 |
| injection          |    0.44 |
