# Real task comparison

Reuse `cost.jsonl`, runtime evidence and `report --root ROOT --output REPORT.md`; do not build a second accounting service. Keep one state root per comparison arm so `aggregate_metrics` remains attributable. Use the same actual objective, starting repository revision, acceptance criteria and permission profile for Team and single `gpt-6.1-sol / medium` runs. Single-Sol measurements must come from a real native/CLI run with observed identity/usage, not FakeRunner or a copied Team receipt. Preserve external single-Sol usage beside the reports if the native surface does not expose the existing cost adapter; unknown usage stays unknown.

Start with 10–20 real matched tasks. Record case ID, arm, real model/effort, task/runtime IDs, total input/output/cached consumption, elapsed time, first-pass acceptance, reworks, missed defects, blocked environment runs and final checks. Separate API monetary cost from subscription quota; missing price/usage is not zero. Environment-blocked cases are not implementation reworks and do not demonstrate model quality.

Reports describe measured consumption; do not claim savings without matched evidence. Existing optimization gates remain shadow by default and cannot be opened by narrative claims. Lower Luna efforts may be tried only in separately bounded, explicitly selected experiments with the same acceptance/evidence and stop conditions; keep production Luna max unchanged.

## Model evidence and experimental priorities

Use the [official Terra introduction](https://openai.com/index/introducing-gpt-6-1-terra/) and [official model guide](https://developers.openai.com/api/docs/guides/latest-model) as the capability frame: Terra for visual/computer-use/natural-writing and routine work, Sol for complex coding, Astra for the hardest reasoning, Luna for focused mechanical batches. Medium is the Terra starting effort, not evidence that it should own complex engineering decisions.

The [Codex Radar ratings](https://codexradar.com/#model-ratings), inspected 2026-10-29, provide experimental priorities, not an engineering ranking. Its four tasks concern historical topics, an animal and candy rather than software engineering; community ratings have small, unequal samples. Costs are API-equivalent estimates excluding judging and failures, not subscription usage or total workflow cost. Consequently keep Sol medium engineering and Sol high acceptance, production Luna max, and Astra's limited role. Terra max is only an explicitly chosen candidate for complex visual/computer-use trials; do not add an automatic production route or claim a measured advantage before matched real tasks.

Additional public evidence checked 2026-10-29 (different suites and efforts are not interchangeable):

| Source | What it informs | Limitation |
|---|---|---|
| [Bito, 2026-10-28](https://bito.ai/blog/gpt-6-1-terra-vs-gpt-6-1-sol-vs-gpt-6-luna/) | Six mixed medium-effort tasks favor Sol for long-session planning; consistent with keeping engineering decisions on Sol | Small sample, Opus 5.5 judge, mixed historical Sol samples; scores are not automatic functional pass rates and 1–2 points are noise. Does not evaluate production Luna max |
| [Leanzero](https://leanzero.net/blog/gpt-6-1-terra-benchmark) | Terra medium checkpoint/reconcile failures support retaining state-machine/concurrency work on Sol | One Gauntlet and two Forge runs; not a general ranking |
| [Plotly](https://plotly.com/blog/gpt-6-1-terra-plotly-data-analytics-bench/) | Terra completes 40/43 domain tasks versus Sol 43 and Luna 38 | Data-analytics specialization is not complex coding capability |
| [Artificial Analysis: Terra / Astra medium](https://artificialanalysis.ai/models/comparisons/gpt-6-1-terra-medium-vs-gpt-6-astra-medium) | Index 38 versus 51; illustrates the capability/cost tradeoff | Nine-evaluation composite, not workflow cost; Terra max index 51 must not be reported as Terra medium |
| [Artificial Analysis: Terra medium / Luna max](https://artificialanalysis.ai/models/comparisons/gpt-6-1-terra-medium-vs-gpt-6-luna) | Composite 38/28, task cost $0.17/$0.15 and time 119/623 seconds: low token price does not guarantee low task cost | Matched effort labels differ by model; no obligation to dispatch Luna or inference of savings on this workflow |
| [Artificial Analysis: Sol / Astra medium](https://artificialanalysis.ai/models/comparisons/gpt-6-1-sol-medium-vs-gpt-6-astra-medium) | Composite 46/51, TB4 48%/49%, task cost $0.36/$2.73 support selective Astra use | Cross-benchmark aggregate costs, not this workflow's observed charges |
| [Vals: Luna](https://vals-ai.com/models/openai_gpt-6-luna) | Actual max-effort page reports Vals 52.32, code migration 46.67 and TB4 13.64 | Current page rather than stale search snippets; not grounds to lower production effort or expand engineering ownership |

These are directional external observations, not local measurements or a cost-saving claim. The production division follows official capability positioning; the external results prioritize explicit, bounded trials only.
