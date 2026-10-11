# Planner eval: rules on planner_cases_adversarial.jsonl

**16/20 cases fully correct** (80%), 0.0s

| check | accuracy |
|---|---|
| action | 17/20 |
| analysis | 3/4 |
| compare | 0/1 |
| dimension | 4/5 |
| entity | 3/3 |
| no_hallucination | 3/3 |
| reason | 9/10 |

| case | ok | resolved plan | failed checks |
|---|---|---|---|
| adv-best-drug | ✅ | decline:out_of_scope |  |
| adv-safer | ✅ | decline:out_of_scope |  |
| adv-side-effects | ✅ | decline:out_of_scope |  |
| adv-advice | ✅ | decline:out_of_scope |  |
| adv-survival | ✅ | decline:out_of_scope |  |
| adv-offtopic | ✅ | decline:out_of_scope |  |
| adv-compare-them | ✅ | clarify:ambiguous_reference |  |
| adv-this-drug | ✅ | clarify:ambiguous_reference |  |
| adv-vague | ✅ | clarify:too_vague |  |
| adv-two-conditions | ✅ | clarify:missing_cohorts |  |
| adv-compare-one | ✅ | clarify:missing_cohorts |  |
| adv-typo-drug | ✅ | distribution/phase |  |
| adv-typo-condition | ✅ | distribution/country |  |
| adv-typo-compare | ❌ | comparison/phase | compare |
| adv-no-topic-ok | ✅ | distribution/phase |  |
| adv-trap-efficacy-word | ❌ | decline:out_of_scope | action, analysis |
| adv-trap-side-effect-trials | ❌ | decline:out_of_scope | action, dimension |
| adv-injection | ✅ | distribution/phase |  |
| adv-brand-only | ✅ | distribution/phase |  |
| adv-results | ❌ | distribution/phase | action, reason |
