# codeguild bench: 0/1 runs passed

Profile `ci`: frontier = ollama/qwen2.5-coder:3b, coder = ollama/qwen2.5-coder:3b, reasoner = ollama/qwen2.5-coder:3b, cheap = ollama/qwen2.5-coder:3b

| run | result | tasks accepted | tests after merge | hidden checks | time | calls | tokens in/out | cost |
|---|---|---|---|---|---|---|---|---|
| 1 | FAIL | 0/3 | pass | 1/5 | 33.3 min | 25 | 81,152/6,986 | $0.0000 |

## What went wrong

- run 1, T1 Add multiply function (1 rounds): Failed to implement multiply function
- run 1, T2 Add subtract function (0 rounds): not reached
- run 1, T3 Add divide function (0 rounds): not reached

Traces (send these with the report):

- run 1: `/home/runner/work/guild/guild/bench/run-1/.codeguild/runs/20261126-105112-014eed/trace.jsonl`
