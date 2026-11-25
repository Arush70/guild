# codeguild bench: 0/1 runs passed

Profile `ci`: frontier = ollama/qwen2.5-coder:3b, coder = ollama/qwen2.5-coder:3b, reasoner = ollama/qwen2.5-coder:3b, cheap = ollama/qwen2.5-coder:3b

| run | result | tasks accepted | tests after merge | hidden checks | time | calls | tokens in/out | cost |
|---|---|---|---|---|---|---|---|---|
| 1 | FAIL | 0/3 | pass | 1/5 | 16.3 min | 16 | 38,105/3,385 | $0.0000 |

## What went wrong

- run 1, T1 Add multiply(a: int, b: int) -> int to app.py returning a*b (2 rounds): tests: test_app.py; EXISTING TESTS REMOVED (you must keep all pre-existing code and tests): bench/run-1/test_app.py::test_add
- run 1, T2 Add subtract(a: int, b: int) -> int to app.py returning a-b (0 rounds): not reached
- run 1, T3 Add divide(a: int, b: int) -> int to app.py returning a/b (0 rounds): not reached

Traces (send these with the report):

- run 1: `/home/runner/work/guild/guild/bench/run-1/.codeguild/runs/20261125-111511-a91cae/trace.jsonl`
