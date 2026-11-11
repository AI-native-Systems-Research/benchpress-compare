# Read the result from a script

Every report write also leaves a small `<results-root>/reports/result.json`
next to `summary.json`. It carries the two headline scores and one `state` that
says whether they can be used, so a search loop or a script can read the outcome
without parsing `summary.json`. The same JSON is printed for any results
directory, finished or not, by:

```bash
uv run benchpress-compare result --results-root ./results/lsf-small
```

A finished run looks like this (numbers rounded here):

```json
{
  "schema_version": 1,
  "state": "complete",
  "scored": true,
  "N2": 1.0793,
  "D2": 0.7149,
  "partial": null,
  "explanation": "All 264 planned observations succeeded; N2 and D2 are the headline scores over 44 circuits.",
  "run_status": "COMPLETE",
  "profile": "evolve",
  "design": "benchpress-evolve-v1",
  "score_scope": "subset_search_signal",
  "plan_hash": "44fe2915...",
  "query": {},
  "observations": {"planned": 264, "completed": 264, "succeeded": 264},
  "dropped_inputs": 0,
  "updated_at": 1794322126.83
}
```

| Field | Meaning |
| --- | --- |
| `state` | One of the values in the table below. |
| `scored` | `true` only when `N2` and `D2` are the main scores. Check it before using them. |
| `N2`, `D2` | `evolve / baseline`; below 1 is better. `null` unless `scored` is `true`. |
| `partial` | Set only in state `evolve_failed`: N2 and D2 over the circuits evolve did not fail on, with the number of circuits (`excluded_inputs`) and the share of the total weight (`excluded_weight`, 0 to 1) it leaves out. |
| `explanation` | One or two sentences with the numbers behind `state`. |
| `run_status` | The controller state, as printed by `benchpress-compare status`. |
| `profile`, `design`, `score_scope`, `plan_hash` | Which benchmark produced the numbers. `score_scope` is `benchmark` for `full` and `subset_search_signal` for `evolve` and `small`. |
| `query` | Slice filters the report was built with; `{}` for the whole benchmark. |
| `observations` | Planned, completed and succeeded observation counts; `null` before a plan exists. |
| `dropped_inputs` | Inputs left out of the score because their baseline failed. |
| `updated_at` | Unix time of the last run-state change. |

`state` takes one of these values, listed in the order a run goes through them:

| `state` | `scored` | Meaning | What to do |
| --- | --- | --- | --- |
| `not_started` | no | The results directory has no run state yet. | Start the run. |
| `preparing` | no | Builds, fixtures and the plan are being prepared. | Wait. |
| `frozen` | no | The plan is frozen but collection has not started. | `resume` the run to collect. |
| `running` | no | Observations are being collected; the explanation gives the progress. | Wait and poll again. |
| `preparation_failed` | no | A build, fixture or planning step failed; the explanation gives the reason. | Fix the cause and start again in a new results directory. |
| `interrupted` | no | The controller was interrupted; committed results are kept. | `resume` the run. |
| `needs_reconciliation` | no | Some scheduler jobs are in an uncertain state. | `resume` the run; it reconciles them first. |
| `cancelled` | no | The run was cancelled before every observation was collected. | `resume --retry-cancelled` to collect the rest, or discard the run. |
| `complete` | yes | Every planned observation succeeded. | Use `N2` and `D2`. |
| `complete_with_exclusions` | yes | The baseline failed on `dropped_inputs` inputs, which were left out with zero weight; the scores cover the remaining circuits. | Use `N2` and `D2`; check the exclusions in `report.md`. |
| `evolve_failed` | no | Evolve failed where the baseline succeeded, so the main score is unavailable. | Treat the candidate as failed. `partial` gives the score without the failed circuits, and `report.md` lists them. |
| `unscored` | no | Collection ended but no score could be computed, for example because a circuit family ended up empty. | Read the explanation and the failure cases in the README's [Read results](../README.md#read-results). |

A run in a `scored` state may still have had failures on dropped inputs; the
explanation says so, and `report.md` lists them.
