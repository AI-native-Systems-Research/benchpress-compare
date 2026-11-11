# Keeping the LSF controller at `max_jobs`

The controller dispatches far fewer jobs than the site allows. On the CCC cluster
with `max_jobs = 100`, the `full` profile run of 2026-11-10 oscillated between
~100 and ~3 running shards on a 15–20 minute cycle, and its throughput fell from
~65 observations/min at the start to ~17/min two hours in. This document explains
why, and gives step-by-step instructions for the fix, in priority order. All
numbers below were measured on that run from `results/full/state/events.jsonl`.

The controller is [orchestrator.py](../src/benchpress_compare/orchestrator.py)
(`ExecutionEngine.run`); the journal is
[journal.py](../src/benchpress_compare/journal.py). This document expands
findings F5 and F34 in [code_review_findings.md](code_review_findings.md).

**Current status (2026-11-11):** the analysis below describes the original
sequential controller. The current loop polls, collects up to 500 observations,
then dispatches for at most `poll_seconds` (plus completion of its current batch).
It saves scheduler state only when it changes, submits with `submit_workers`
threads, and publishes with `collect_workers` threads; both default to 8.
Journal appends and central-state publications remain serialized by file locks.

The checked-in site configuration already allows 150 jobs and 150 CPUs, with
a 2,000 GiB worker memory budget. The observed roughly 50-job run used the older
controller, before commit `903114d`. Use a fresh results directory to run the
updated controller: requests and plans pin its source hash, so an old run cannot
resume with this implementation. Keep the existing shared store for reusable
baseline results.

Concurrency tests cover job, CPU and memory bounds, cancellation during batch
preparation, interrupted submissions, and concurrent publication under lease
takeover. Actual sustained LSF occupancy still needs a new cluster run: short
shards require more frequent replenishment, and filesystem and scheduler latency
can keep running jobs below the configured ceiling. `max_jobs` includes pending
and suspended jobs as well as running ones.

## 1. Diagnosis

### 1.1 The loop is sequential and does its work in phases

One iteration of `ExecutionEngine.run` (orchestrator.py, the `while pending or
active` loop) does, in this order:

1. Honour a cancel request.
2. **Dispatch**: launch pending shards until it counts `max_jobs` owned jobs or
   runs out of pending work.
3. **Poll** the scheduler once (`executor.poll_many`).
4. **Collect**: for every attempt whose job is terminal, validate the receipt and
   commit the result, one after another.
5. Sleep `min(poll_seconds, 30)` seconds.

Nothing is collected during step 2 and nothing is dispatched during step 4. The
dispatch budget in step 2 counts every attempt still in `active`, and an attempt
only leaves `active` when its result is committed in step 4. So a shard that
finished on LSF five minutes ago still occupies one of the 100 slots until the
controller gets round to committing it.

Measured on the run: dispatch phases of 6–13 minutes with zero commits, then
commit phases of 4–8 minutes with zero submissions. Shards of six small
observations finish in 3–5 minutes, so by the end of each dispatch phase most of
the shards it launched are already done, and during each commit phase the cluster
drains to a handful of jobs.

### 1.2 Every journal append re-verifies the whole journal

`Journal.append` (journal.py) calls `self._read()` before writing, which parses
every line of `state/events.jsonl` and recomputes every event hash to find the
sequence number, previous hash and committed byte count of the tail. That is
correct, but its cost grows with the run:

| Events in journal | Bytes | `_read()` per append |
| ---: | ---: | ---: |
| 10,340 (57% of the run) | 13.5 MB | 0.46 s |
| ~17,000 (end of run, extrapolated) | ~22 MB | ~0.75 s |

Each observation causes three appends (`submission_intent`, `submitted`,
`result_committed`), so at the 57% mark the journal alone costs ~1.4 s of
controller time per observation. The measured costs line up with that:

- launching one shard of six observations: median 4.5 s (12 appends);
- committing one result: median 0.68 s (1 append plus hashing and publishing).

Summed over the run this quadratic behaviour is about 1.7 hours of controller
CPU, which matches the run's whole duration: the controller, not the cluster, is
the bottleneck, and it gets slower as the run progresses.

### 1.3 What is *not* the problem

- `fsync` on the results filesystem (GPFS) is cheap: 0.4 ms for an append and
  1.1 ms for an `atomic_write_json`. Do not remove durability to go faster.
- Scheduler commands are fast: the per-cycle `bjobs -a ... -J "bpc-<run>-*"`
  listing takes 0.6 s with 700 records, `bsub` well under a second, and
  `CLEAN_PERIOD` on this site is 14 days so the slow `bhist` fallback is never
  taken.

## 2. The fix, in priority order

Do step 2.1 first; it removes most of the controller time per observation. Step
2.2 then lets the controller use that time to keep the cluster full. Step 2.3 is
optional polish.

### 2.1 Make `Journal.append` O(1) by caching the tail

Keep the tail state in memory after the first full verification and reuse it for
later appends by the same `Journal` instance. The hash chain, the torn-tail rule
(an unterminated last line is not committed) and the on-disk format do not
change, so `replay()`, `checkpoint()` and any reader of `events.jsonl` are
unaffected. One guarantee does narrow. Before, `append` re-verified the whole
chain on every call, so it also caught an in-place edit to an earlier committed
event. Now it re-verifies only when the file's stat signature changes, so an
edit that keeps the size and lands in the same mtime tick is caught by
`replay()` and by a new `Journal` (a restarted controller), not by the next
append on the same instance. `test_committed_corruption_is_rejected` checks it
that way.

In `Journal.__init__` add:

```python
self._tail: tuple[int, str | None, int] | None = None  # (count, last hash, bytes)
```

In `append`, inside the existing `with ownership, _guard(...)` block, replace the
unconditional `events, committed_bytes = self._read()` with:

```python
tail = self._tail
try:
    size = self.path.stat().st_size
except FileNotFoundError:
    size = None
if tail is None or size != tail[2]:
    # First append, a torn tail, or a write by another generation: verify
    # the whole chain exactly as before.
    events, committed_bytes = self._read()
    tail = (
        len(events),
        events[-1]["event_hash"] if events else None,
        committed_bytes,
    )
count, previous, committed_bytes = tail
```

Build the event from `count` and `previous` instead of `len(events)` and
`events[-1]["event_hash"]`, write it exactly as now (`truncate(committed_bytes)`,
append, `flush`, `fsync`), then record the new tail before returning:

```python
self._tail = (count + 1, event["event_hash"], committed_bytes + len(line))
```

where `line` is the encoded bytes that were written. Keep the
`fsync_directory` call; it costs nothing here.

Why this is safe:

- The guard lock and the lease fence already make appends by the owning
  generation sequential, so the cached tail cannot go stale through a concurrent
  owner. Any write by someone else, or a crash mid-append, changes the file size
  and forces the full verification path, which also truncates a torn tail.
- A cached tail is only ever derived from a fully verified read or from an
  append this instance just made and fsynced.

Tests to add in `tests/test_journal.py`:

- Two `Journal` instances on the same path: instance A appends, instance B
  appends, instance A appends again. `replay()` must return three events with
  sequences 0, 1, 2 and an intact chain (A's cache was invalidated by the size
  change).
- After a torn tail is written directly to the file, the next append must
  truncate it and continue the chain (reuse the pattern of
  `test_replay_and_torn_tail_recovery`).
- Performance guard: monkeypatch `Journal._read` to count calls; 200 appends on
  one instance must call it once.

Expected effect: `append` drops from ~0.5 s to ~1 ms; launching a shard from
~4.5 s to well under 0.5 s; a commit from ~0.68 s to ~0.2 s (the remainder is
receipt validation, artifact hashing and bundle publishing).

### 2.2 Interleave dispatch with collection and stop counting finished jobs

Change the loop in `ExecutionEngine.run` so that each iteration does a bounded
amount of dispatch and the dispatch budget reflects what is actually on the
scheduler.

**a. Reorder the iteration** to: cancel check → poll → collect → raise on
uncertain or harness failures → dispatch → sleep. Polling first means the
dispatch budget uses fresh job statuses, and an uncertain poll now stops
dispatch in the same iteration instead of the next one, which is what the
"no replacements submitted" rule intends.

**b. Exclude scheduler-terminal jobs from the budget.** Where `owned_jobs`,
`used_cpu`, `used_memory` and `job_count` are computed, only count attempts
whose job is still on the scheduler:

```python
SCHEDULER_TERMINAL = {"DONE", "EXIT", "INFRA_FAILED", "RESOURCE_LIMIT", "CANCELLED"}
owned_jobs = {
    attempt["job"]["token"]: spec
    for spec, attempt in active.values()
    if attempt["job"]["status"] not in SCHEDULER_TERMINAL
}
```

Keep counting `QUEUED`, `RUNNING`, `SUBMITTING`, `CANCELLING`,
`SUBMISSION_UNKNOWN` and `NEEDS_RECONCILIATION`: pending and suspended jobs
hold scheduler resources, and uncertain ones must be assumed to. The site
config comment "Controller dispatch budgets include pending and suspended jobs"
stays true.

With the order from (a) and unbounded collection, every finished attempt is
committed and removed from `active` before dispatch, so this filter changes
nothing. It matters once collection is capped (e): attempts left uncommitted
then stop holding dispatch slots. Keep it anyway, so the budget does not
depend on how collection is bounded.

**c. Time-box the dispatch slice.** Before the `for spec in list(pending)`
loop set `deadline = time.monotonic() + self.poll_interval`, and inside it
`break` once `time.monotonic() >= deadline`. On LSF that is 20 s per
iteration, enough for several shards once 2.1 is in place; locally
`poll_interval` is 0.1 s and launches take milliseconds, so the slice is still
large. Remember whether the slice ended on the deadline with pending work and
free capacity.

**d. Sleep only when idle.** Replace the unconditional
`time.sleep(min(self.poll_interval, 30))` with: sleep only if this iteration
launched nothing and committed nothing (or the slice did not hit its deadline).
Otherwise loop immediately so a backlog is worked off at full speed.

**e. Optionally bound collection too.** If one poll returns hundreds of
finished attempts, committing all of them still blocks dispatch for a while.
Processing at most, say, 50 per iteration is enough: attempts left in
`active` with a `DONE`/`EXIT` job are returned by `poll_many` without a
scheduler query, so they are picked up next iteration at no cost.

Invariants that must survive the change (all are enforced by existing tests in
[test_orchestrator.py](../tests/test_orchestrator.py); run the whole suite):

- `submission_intent` and `attempt.json` are durable before `bsub`; `job.json`
  is written before the `submitted` events (`_launch_bundle` is unchanged).
- A shard is launched only from observations still in `pending`, and the frozen
  bundle plan is never split differently (`bundle_by_key` logic unchanged).
- `lease.assert_owned()` once per iteration.
- `ReconciliationError` on uncertain jobs and `PreparationError` on harness
  failures are raised before any further dispatch.
- The cancel request is honoured at the top of every iteration.

Tests to add in `tests/test_orchestrator.py`:

- Interleaving: 30 specs, `workers="2"` (local) or `max_jobs=2` (FakeLSF), a
  fake worker that finishes instantly. In the journal, `result_committed`
  events must appear between `submitted` events rather than all submissions
  first. Assert the first `result_committed` precedes the last `submitted`.
- Budget excludes finished jobs: with `max_jobs=1`, after the first job's
  process has exited but before the controller commits it, a second launch must
  be allowed. The simplest form is to extract the budget computation into a
  method, e.g. `_scheduler_load(active) -> (jobs, cpus, memory)`, and unit-test
  it with attempts in `RUNNING`, `DONE` and `SUBMISSION_UNKNOWN`.
- `test_unknown_local_submission_is_reconciled_without_duplicate` must still
  raise before anything is launched.

### 2.3 Optional: parallel artifact work in collection

After 2.1 and 2.2 a commit is dominated by hashing and copying artifacts
(`_commit` → `publish_bundle`, and `store.publish` for verified baselines). Those
are independent across observations; a small thread pool can hash and stage
bundles while the main thread keeps the journal and `publish_bundle` calls in
order. Only consider this if, after 2.1 and 2.2, `bjobs` still shows fewer than
`max_jobs` jobs for long stretches. The journal must stay single-writer.

## 3. Verifying the result

Run the unit tests, then a real LSF run (the `small` profile is enough) and
check three things while it runs.

**a. Jobs on the scheduler stay near `max_jobs`:**

```bash
watch -n 30 'bjobs -noheader -o stat | sort | uniq -c'
```

After the first fill, `RUN + PEND` should stay close to `max_jobs` until pending
work runs out, not swing between the cap and single digits.

**b. Dispatch and collection are interleaved.** Per-minute counts from the
journal should show both kinds of events in most minutes:

```bash
python3 - <<'E'
import json, time, collections
c = collections.Counter()
for line in open("results/<run>/state/events.jsonl"):
    e = json.loads(line)
    if e["event_type"] in ("result_committed", "submitted"):
        c[(time.strftime("%H:%M", time.localtime(e["timestamp"])), e["event_type"])] += 1
for m in sorted({m for m, _ in c}):
    print(m, "commit=%3d submit=%3d" % (c[(m, "result_committed")], c[(m, "submitted")]))
E
```

**c. Per-event costs are flat.** Median gap between consecutive commits should be
~0.2 s or less and not grow over the run; the intent→submitted span of a shard
should be well under a second:

```bash
python3 - <<'E'
import json, statistics, collections
ev = [json.loads(l) for l in open("results/<run>/state/events.jsonl")]
intent = {e["data"]["attempt_id"]: e["timestamp"] for e in ev if e["event_type"] == "submission_intent"}
spans = collections.defaultdict(list)
for e in ev:
    if e["event_type"] == "submitted" and "bundle_id" in e["data"]:
        spans[e["data"]["job"]["token"]].append((intent[e["data"]["attempt_id"]], e["timestamp"]))
per_shard = [max(t for _, t in s) - min(t for t, _ in s) for s in spans.values()]
commits = [e["timestamp"] for e in ev if e["event_type"] == "result_committed"]
gaps = [b - a for a, b in zip(commits, commits[1:]) if b - a < 5]
print("median shard launch %.2fs, median commit gap %.2fs" % (statistics.median(per_shard), statistics.median(gaps)))
E
```

Before the fix on the 2026-11-10 run: 4.5 s and 0.68 s.

## 4. Rollout notes

- Any change under `src/benchpress_compare/` changes `harness_identity()`, and a
  run whose plan was frozen with the old hash can no longer be resumed
  (`verify_plan` and `_request_config` refuse it). Land this between runs, or
  develop it in a separate git worktree so the editable install used by a
  running controller is untouched, and merge after that run completes.
- The harness wheel hash is part of the build identity, so observation ids
  change too and the store's baseline cache will not be reused by the next run.
  That is already true of any harness change; it is not specific to this one.
- No journal, bundle, receipt or worker-protocol format changes: existing
  `results/` directories remain readable by `status`, `report` and `export`.
