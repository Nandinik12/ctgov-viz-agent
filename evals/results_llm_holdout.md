# Planner eval: llm on planner_cases_holdout.jsonl

**15/15 cases fully correct** (100%), 38.4s

| check | accuracy |
|---|---|
| analysis | 15/15 |
| compare | 2/2 |
| dimension | 12/12 |
| entity | 3/3 |
| filters | 5/5 |
| no_hallucination | 1/1 |
| secondary_dimension | 1/1 |

| case | ok | resolved plan | failed checks |
|---|---|---|---|
| h-trend-1 | ✅ | time_trend/start_year |  |
| h-trend-2 | ✅ | time_trend/start_year |  |
| h-dist-1 | ✅ | distribution/phase |  |
| h-dist-2 | ✅ | distribution/sponsor |  |
| h-dist-3 | ✅ | distribution/intervention_type |  |
| h-geo-1 | ✅ | distribution/country |  |
| h-geo-2 | ✅ | distribution/country |  |
| h-cmp-1 | ✅ | comparison/sponsor_class |  |
| h-cmp-2 | ✅ | comparison/phase |  |
| h-hist-1 | ✅ | histogram/enrollment |  |
| h-net-1 | ✅ | network/drug |  |
| h-net-2 | ✅ | network/condition |  |
| h-status-1 | ✅ | distribution/sponsor |  |
| h-ground-1 | ✅ | distribution/phase |  |
| h-scatter-1 | ✅ | scatter/enrollment |  |
