# Planner eval: llm on planner_cases.jsonl

**30/30 cases fully correct** (100%), 76.0s

| check | accuracy |
|---|---|
| analysis | 29/29 |
| compare | 4/4 |
| dimension | 22/22 |
| entity | 8/8 |
| filters | 12/12 |
| no_hallucination | 2/2 |
| secondary_dimension | 3/3 |
| series_dimension | 1/1 |

| case | ok | resolved plan | failed checks |
|---|---|---|---|
| trend-drug-since | ✅ | time_trend/start_year |  |
| trend-structured | ✅ | time_trend/start_year |  |
| trend-condition | ✅ | time_trend/start_year |  |
| trend-split-phase | ✅ | time_trend/start_year |  |
| trend-range | ✅ | time_trend/start_year |  |
| dist-phase | ✅ | distribution/phase |  |
| dist-intervention-type | ✅ | distribution/intervention_type |  |
| dist-status | ✅ | distribution/overall_status |  |
| dist-sponsor | ✅ | distribution/sponsor |  |
| dist-sponsor-class | ✅ | distribution/sponsor_class |  |
| dist-drugs | ✅ | distribution/drug |  |
| dist-conditions | ✅ | distribution/condition |  |
| geo-recruiting | ✅ | distribution/country |  |
| geo-phase3 | ✅ | distribution/country |  |
| cmp-drugs-phase | ✅ | comparison/phase |  |
| cmp-structured | ✅ | comparison/sponsor_class |  |
| cmp-status | ✅ | comparison/overall_status |  |
| cmp-trend | ✅ | comparison/start_year |  |
| hist-enrollment | ✅ | histogram/enrollment |  |
| hist-duration | ✅ | histogram/duration_months |  |
| scatter | ✅ | scatter/enrollment |  |
| net-sponsor-drug | ✅ | network/sponsor |  |
| net-drug-drug | ✅ | network/drug |  |
| net-condition-drug | ✅ | network/condition |  |
| net-sponsor-country | ✅ | network/sponsor |  |
| ground-no-brand | ✅ | distribution/phase |  |
| ground-no-invent | ✅ | distribution/phase |  |
| struct-wins | ✅ | distribution/phase |  |
| status-completed | ✅ | distribution/sponsor |  |
| study-type | ✅ | distribution/study_type |  |
