# Controller throughput collapse on the full 10-seed run

Prepared 2026-11-11. **Status: diagnosed and fixed in the working tree (uncommitted); the affected run was aborted and relaunched with the fix at 05:49 EST.**

The LSF controller for `results/full_new_weight_10_seeds` (full profile, 10 seeds, 17,640 observations) slowed from about 60 observations per minute to 25 within an hour of starting measurement, while keeping only about 19 of its allowed 100 jobs running. The cluster was not the limit. The controller was spending most of each loop rewriting bookkeeping files for finished jobs it had not yet collected, and that backlog grew on every iteration because collection was capped below what one dispatch window submits. Two small changes in `orchestrator.py` fix it; the run had to be restarted in a fresh results directory because the coordinator hash is pinned into the frozen plan.

## 1. What was observed

Measured from `state/events.jsonl`, the monitor log and `bjobs`. Times are EST.

| Time | Observations done | Shard jobs running | Controller loop period | Observation rate |
|---|---|---|---|---|
| 04:47 | 50 | 81 | ~45 s | – |
| 05:20 | 1,923 | 91 | ~45 s | ~60/min |
| 05:27 | 2,300 | 58 | ~60 s | ~60/min |
| 05:33 | 2,550 | 38 | ~115 s | ~40/min |
| 05:38 | 2,703 | 19 (3 of them with uncollected work) | ~120 s | ~25/min |

At 05:38 the run had submitted 1,148 shard jobs carrying 5,633 observations, committed 2,703 of them, and was holding **3,067 observations from 611 jobs that LSF had already finished** without collecting them. Only 13 observations were actually running. None of the run's jobs were pending in LSF, the `normal` queue allows 5,000 jobs per user, and the site budgets in `lsf-site.toml` (`max_jobs = 100`, `max_cpus = 120`, `max_memory_gb = 2000`, that is 181 jobs at 11 GB) were nowhere near binding.

## 2. Where one iteration spent its time

One controller iteration, 05:29:33 to 05:31:36 (123 s), reconstructed from event timestamps:

| Phase | What it does | Time | Output |
|---|---|---|---|
| Poll | One `bjobs -a -json -J 'bpc-<prefix>-*'` listing of all jobs so far | ~1–2 s | fresh job states |
| Re-save | Rewrite `attempt.json` for every active attempt, finished or not | **81.6 s** (silent; ~3,080 fenced writes at ~26 ms) | nothing new |
| Collect | Validate receipts and publish results, capped at `COLLECT_PER_ITERATION = 50` observations | 18 s (~0.35 s each) | 50 observations |
| Dispatch | Create attempt directories, write shard manifest, `bsub`, bounded by a `poll_seconds = 20` wall-clock deadline | 20.3 s (~1.1 s per shard, of which `bsub` is 0.4–1 s) | 18 shards, 108 observations |

At 05:20 the same loop took ~45 s because the re-save phase was still short. Shard jobs run for about two minutes, so 18 submissions per two-minute iteration keeps roughly 19 jobs in flight, which is exactly what LSF showed.

## 3. Root cause

Three properties of `ExecutionEngine.run` combined:

1. **Every active attempt was re-saved every iteration.** The collection loop updated and wrote `attempt.json` for each entry in `active` before checking whether anything had changed, including attempts whose job had finished but were waiting past the collection cap. Each write is a fenced atomic write on the shared filesystem (`_save_attempt`, [orchestrator.py:175](../../src/benchpress_compare/orchestrator.py#L175)).
2. **Collection was capped below dispatch.** Collection stopped after 50 observations per iteration ([orchestrator.py:574](../../src/benchpress_compare/orchestrator.py#L574)), while the 20 s dispatch window ([orchestrator.py:707](../../src/benchpress_compare/orchestrator.py#L707)) submits about 18 shards of up to 6 observations (`shard_max_observations = 6`, 5.1 on average), about 90–108 observations. The uncollected backlog therefore grew by roughly 40 observations per iteration from the first minute of measurement.
3. **Finished-but-uncollected jobs do not count against the dispatch budget** (`_scheduler_load`, [orchestrator.py:225](../../src/benchpress_compare/orchestrator.py#L225)), so nothing throttled dispatch as the backlog grew.

Together these made the loop quadratic: each iteration added ~40 attempts to the backlog, each of which added ~26 ms to every later iteration. Because dispatch is bounded by wall time rather than by job count, a slower loop directly means fewer submissions per minute and fewer running jobs. The running-job count would have kept falling until the number of observations submitted per iteration dropped to 50.

## 4. Fix

Both changes are in [`src/benchpress_compare/orchestrator.py`](../../src/benchpress_compare/orchestrator.py). The full test suite passes (297 tests) and `ruff check`/`ruff format` are clean.

| Change | Location | Effect |
|---|---|---|
| Save `attempt.json` only when the polled job dict or status differs from what is already stored | [orchestrator.py:564](../../src/benchpress_compare/orchestrator.py#L564) | Removes the re-save phase; finished attempts waiting for collection cost nothing per iteration. Restarted controllers still find the last scheduler state on disk because every transition is still written. |
| `COLLECT_PER_ITERATION` 50 → 250, with a comment tying it to the dispatch window | [orchestrator.py:31](../../src/benchpress_compare/orchestrator.py#L31) | Collection can always keep up with what one dispatch window submits, so a backlog cannot build in steady state. The existing 3,000-observation backlog would have drained in ~15 iterations while dispatch continued. |

Expected steady state after the fix, with two-minute shards: poll ~1 s, collect ~90 observations in ~32 s, dispatch 18 shards in 20 s, so an iteration of ~55 s, about 20 submissions per minute, about 40 jobs running, and roughly 100 observations per minute. A full 10-seed run should take about 3 hours of measurement instead of the 10 or more it was heading for.

## 5. Why the run could not be resumed

The coordinator's source hash (`harness_identity()`, every `.py` under the package plus the catalogue and profiles) is pinned in `request.json` and inside the frozen `plan.json`, whose own hash covers it. Any coordinator change therefore fails both gates:

- `submit_controller` refuses with "Coordinator implementation changed after launch; use a new results directory" ([controller.py:34](../../src/benchpress_compare/controller.py#L34)).
- `verify_plan` raises "Frozen worker protocols or coordinator implementation changed; use a new run" ([orchestrator.py:57-62](../../src/benchpress_compare/orchestrator.py#L57-L62)). Confirmed against the old root: pinned `b78fe8f0…`, current `30327acf…`.

This is intentional provenance protection, so the run was restarted rather than its hashes rewritten. Any future coordinator fix has the same cost: the 2,703 committed observations of the old run cannot be carried forward.

## 6. Relaunch record

| Step | Detail |
|---|---|
| Old controller | LSF job 1787477, launched 04:23, killed 05:48 with `bkill`; remaining shards killed with `bkill -J 'bpc-0eba2a91-*'` |
| Old results | Moved to `results/full_new_weight_10_seeds.aborted-20261111` (status `INTERRUPTED`, 2,703 committed observations, all `SUCCEEDED`) |
| New controller | LSF job 1801749, launched 05:49 with the unchanged command from `commands.txt`; `request.json` pins coordinator hash `30327acf…` |
| Monitor | `/tmp/benchpress-monitor-full_new_weight_10_seeds/monitor.py` restarted on the new root; the old `monitor.log`, `history.jsonl` and `latest.json` were renamed `*.aborted-20261111` |
| Job-name prefix | `bpc-0eba2a91-…` is a hash of the results path, so the relaunch reuses it. The old run's finished jobs stay visible in `bjobs -a` for the site's 14-day `CLEAN_PERIOD`; they carry different tokens and are ignored by `poll_many`'s exact-name match. |

## 7. What to check on the new run

- The LSF running-job count should rise well above 20 once measurement starts, and stay there.
- The gap between `submitted` and `result_committed` observations in `state/events.jsonl` should stay in the low hundreds, not grow.
- The monitor's reported rate should be near 100 observations per minute.

## 8. Follow-ups

- Commit the fix; it is only in the working tree.
- Dispatch cost is now the ceiling (~1.1 s per shard: 12 attempt files, a manifest, 12 journal appends and one `bsub`). If more concurrency is wanted, batch the journal appends and attempt writes per shard, or let the dispatch deadline scale with the unused job budget.
- Collection is bounded by count and dispatch by wall time; a single time budget for both would make the balance explicit instead of relying on a constant.
- Consider a check in the controller that logs when `active` holds more finished-than-collected attempts than `COLLECT_PER_ITERATION`, so this pattern is visible in the monitor next time.
