# Code review findings: `src/benchpress_compare`

Review date: 2026-11-10. Scope: every module under `src/benchpress_compare`
(39 files, ~8,600 lines), reviewed against the working tree that became commit
`82be945` ("Fixed memory bug"). That commit includes the changes to `config.py`,
`executors/lsf.py` and `orchestrator.py` that add scheduler `TERM_*`
classification and `memory_headroom_gb`; they were still uncommitted while the
review ran. Nothing was changed; this document only records findings and
suggested fixes.

State at review time: `pytest` passes (267 tests), `ruff check src` is clean.
Two measurements were taken to size performance findings (see F5 and F27). F5
and F34 also use measurements from the live `full` run of 2026-11-10, written up
in [controller_throughput.md](controller_throughput.md).

Severity scale:

- **High**: can abort a long run, permanently lose observations, or misclassify
  a result in a way that changes the score.
- **Medium**: robustness or scaling problems that will show up at full-profile
  scale or under ordinary cluster conditions.
- **Low**: edge cases, hygiene, documentation drift.

Line numbers refer to the files as of commit `82be945`.

---

## High

### F1. A single `HARNESS_FAILED` observation aborts the whole run, is never retryable, and poisons its shard-mates

**Where**

- [orchestrator.py:704-712](../src/benchpress_compare/orchestrator.py#L704-L712): any committed `HARNESS_FAILED` result raises `PreparationError("Worker protocol incompatible: …")` out of `run()`.
- [orchestrator.py:607-614](../src/benchpress_compare/orchestrator.py#L607-L614): a shard member whose `supervisor.json` says `HARNESS_FAILED` is committed as `HARNESS_FAILED`.
- [worker/shard.py:57-61](../src/benchpress_compare/worker/shard.py#L57-L61) and [shard.py:105-107](../src/benchpress_compare/worker/shard.py#L105-L107): once one child reports `HARNESS_FAILED`, every later entry in the shard is written as `HARNESS_FAILED` / `previous_worker_harness_failure` without running.
- [orchestrator.py:726-754](../src/benchpress_compare/orchestrator.py#L726-L754): `collect()` has no exception handling, so the run state file is left at `RUNNING`.

**Problem**

`HARNESS_FAILED` is a terminal status that is published as an immutable bundle, and the only reopen path in the code is `--retry-cancelled`, which reopens `CANCELLED` only. The consequence chain for one harness failure on LSF is:

1. The child writes a `HARNESS_FAILED` receipt (any uncaught exception in `run_spec`, including the cases in F2 and F3).
2. The shard marks all remaining members, including **baseline** observations of other cases, as `HARNESS_FAILED` without running them.
3. The controller commits them all, then raises `PreparationError` and exits with `status.json` still saying `RUNNING`. The `--wait` client reports "Controller ended without a terminal run state".
4. On resume the committed results are returned by `_existing`, so the run continues, but those observations are lost forever. Each new harness failure costs another controller restart.
5. Lost baseline observations drop their inputs from the score; lost evolve observations make the main score unavailable.

Commit `82be945` correctly reclassifies external kills (`-9`, `-15`, `TERM_*`) as `INFRA_FAILED`, which narrows the remaining unexplained-exit path, but the abort-and-poison behaviour for real `HARNESS_FAILED` receipts is unchanged.

**Suggested fix**

- Treat an observation-level harness failure as a committed failure of that observation only. Keep the hard abort for **preparation tasks** (`spec["task"] != "observation"`), where it is the right call.
- In `shard.py`, drop the `harness_failed` propagation: every child is an independent process with its own receipt, so a later child should run regardless (keep only the `stopped` check).
- Add a reopen path for terminal failures, for example `--retry-status HARNESS_FAILED,INFRA_FAILED` generalising the existing `--retry-cancelled` branch at [orchestrator.py:464-475](../src/benchpress_compare/orchestrator.py#L464-L475).
- Wrap `engine.run` in `collect()` so any exception records a terminal state (`FAILED`, `reason=…`), the way `prepare()` does at [preparation.py:651-654](../src/benchpress_compare/preparation.py#L651-L654).

### F2. Any evolve source that edits `sabre/layout.rs` cannot be benchmarked: the run aborts on its first evolve observation

**Where**

- [worker/starts.py:23-26](../src/benchpress_compare/worker/starts.py#L23-L26): exactly one reviewed source hash.
- [starts.py:52-53](../src/benchpress_compare/worker/starts.py#L52-L53): any other hash returns `status: "unsupported"`.
- [worker/adapter.py:172-176](../src/benchpress_compare/worker/adapter.py#L172-L176): `unsupported` raises `AdapterIncompatible`.
- [worker/__init__.py:213-218](../src/benchpress_compare/worker/__init__.py#L213-L218): `AdapterIncompatible` is re-raised and becomes `HARNESS_FAILED`, which then triggers F1.

**Problem**

The start-template attestation is a *diagnostic* (it describes SABRE's implicit starting layouts; it does not enter N2/D2 or the validation contract), yet it is enforced as a hard gate keyed on a single hash of `crates/transpiler/src/passes/sabre/layout.rs`. A candidate change to SABRE layout, which is precisely the kind of transpiler change the `evolve` profile exists to evaluate ("when using in evolve frameworks like Coral"), produces `HARNESS_FAILED` on every evolve observation and the controller aborts after the first batch. The same gate also fires if the *baseline* is ever moved to a Qiskit version with a different `layout.rs`.

**Suggested fix**

- Make an unreviewed source a recorded diagnostic (`diagnostics["native_calls"][i]["starting_layouts"] = {"status": "unsupported", …}`) instead of raising. Only the native-call ABI checks in `native_instrumentation` (signature, trial counts) protect the measurement contract and should stay fatal.
- If the attestation must remain mandatory for the full benchmark design, make it conditional on the profile (`full`) and advisory for `evolve`/`small`, and say so in the report.

### F3. Illegal compiler output is classified as a harness failure instead of `INVALID`

**Where**

- [worker/__init__.py:225-226](../src/benchpress_compare/worker/__init__.py#L225-L226): `export_circuit(compiled)` and `measure(...)` run outside the `try` that classifies compiler failures, and before `target_legality` is evaluated at line 233.
- [metrics.py:99-104](../src/benchpress_compare/metrics.py#L99-L104): `measure` raises `UnsupportedMetric` for a non-native two-qubit gate or any gate on more than two qubits.

**Problem**

If one arm's transpiler leaves a non-native gate (for example a `ccx`, an `ecr`, or a two-qubit gate of kind `definition`) in its output, the worker raises `UnsupportedMetric` or `UnsupportedCircuit` out of `_run_task`, `run_spec` records `HARNESS_FAILED`, and F1 aborts the run. The validation contract already has the right status for this: `target_legality` would return `mismatch` and the observation would be `INVALID`, counting against that arm.

**Suggested fix**

Evaluate `target_legality(serialized, snapshot)` first; only call `measure` when it is `verified`. Catch `UnsupportedMetric`/`UnsupportedCircuit` around `export_circuit` and `measure` and return `{"status": "INVALID", "reason": …, "validation": summarize_validation({...})}` with `N2`/`D2` set to `None`.

---

## Medium

### F4. Concurrent runs sharing a store can abort each other's controller

**Where**

- [store.py:336](../src/benchpress_compare/store.py#L336): `Store.publish` enters a per-key `Lease`; `acquire()` raises `LeaseBusy` immediately ([store.py:94-97](../src/benchpress_compare/store.py#L94-L97)) with no wait.
- [orchestrator.py:367-373](../src/benchpress_compare/orchestrator.py#L367-L373): `store.get` is checked outside the lease, then `store.publish` is called.
- [store.py:269-278](../src/benchpress_compare/store.py#L269-L278): a destination published by someone else with a different manifest raises `IntegrityError("Conflicting immutable publication")`.
- [preparation.py:163](../src/benchpress_compare/preparation.py#L163): the source-snapshot store lease has the same no-wait behaviour.

**Problem**

The documented workflow runs several evolve candidates against one baseline with a shared `--store`. Until the baseline observations are cached, every run measures them. Two runs finishing the same baseline observation within the same publication window hit either `LeaseBusy` (a `ReconciliationError`) or the conflicting-manifest `IntegrityError` (records differ in `attempt_id`, `resources`, …). Both propagate out of `_commit` and kill the controller. The window is short, so this is rare per observation, but a full profile has 2,682 baseline observations per run and the consequence is a dead LSF controller that must be resumed by hand.

**Suggested fix**

In `_commit`, retry the store lease with a bounded wait (seconds to a few minutes) and, after acquiring it, re-check `store.get`: if a complete entry already exists with equal `N2`/`D2`, treat it as success (the check at line 372 already expresses this rule). Apply the same bounded wait to the snapshot lease in `_snapshot_paths`.

### F5. The journal re-reads and re-hashes the entire event chain on every append (quadratic)

**Where** [journal.py:57-86](../src/benchpress_compare/journal.py#L57-L86) calls `_read()` ([journal.py:21-52](../src/benchpress_compare/journal.py#L21-L52)) under the lease fence for each append.

**Measured**: with a representative `submitted` payload, one append costs 3.2 ms at ~100 events and 95 ms at ~1,500 events (fast local disk). The aborted full run in `results/full.aborted-20261110` has 3,251 events for 839 committed observations, so a complete full profile is on the order of 20,000 events: roughly 1.2 s per append near the end and several hours of controller CPU in total, with a ~25 MB file re-read on a shared filesystem each time. Every append holds `lease.fenced()`, so the heartbeat thread is blocked for the duration, which erodes the 120 s lease TTL margin.

The live `full` run of 2026-11-10 on GPFS confirms this: at 10,340 events (13.5 MB, 57 % of the run) one `_read()` takes 0.46 s. Each observation causes three appends, which adds about 1.4 s of controller time per observation. Median shard launch is 4.5 s (12 appends) and median commit is 0.68 s. `fsync` is not the cost (0.4 ms per append), so durability should stay as it is. Together with F34, this is why that run's throughput fell from ~65 to ~17 observations/min.

**Suggested fix** Keep the verified chain state in memory after the first replay (`count`, `last_hash`, `committed_bytes`) and on append only verify that the file size still equals `committed_bytes` (re-replay only if it does not). Optionally move the big `job` dicts out of the `submitted` event (they are already persisted in `attempt.json`). [controller_throughput.md §2.1](controller_throughput.md#21-make-journalappend-o1-by-caching-the-tail) has the step-by-step change and the tests to add.

**Status** Implemented on branch `bug_fixes`: about 0.2 ms per append at 3,000 events, against 0.17 s for one full verification.

### F34. The controller loop serialises dispatch and collection and counts finished jobs against `max_jobs`

**Where** [orchestrator.py:494](../src/benchpress_compare/orchestrator.py#L494) (`while pending or active`): dispatch at [orchestrator.py:520-562](../src/benchpress_compare/orchestrator.py#L520-L562), a single `poll_many` at [orchestrator.py:567](../src/benchpress_compare/orchestrator.py#L567), collection until [orchestrator.py:704](../src/benchpress_compare/orchestrator.py#L704), and an unconditional sleep at [orchestrator.py:713-714](../src/benchpress_compare/orchestrator.py#L713-L714).

**Problem** Each iteration first launches shards until `max_jobs` or pending work runs out, then polls once, then commits every finished attempt. Nothing is collected while it dispatches, and nothing is dispatched while it collects. The dispatch budget counts every attempt in `active`, and an attempt leaves `active` only when it is committed. As a result, a shard that finished on LSF minutes earlier still holds a slot. On the live 2026-11-10 `full` run with `max_jobs = 100`, dispatch phases lasted 6-13 minutes with no commits and commit phases 4-8 minutes with no submissions. Running shards swung between ~100 and ~3 on a 15-20 minute cycle. F5 makes every phase longer as the journal grows.

**Suggested fix** Reorder each iteration to cancel check → poll → collect → raise on uncertain or harness failures → dispatch. Time-box the dispatch slice to `poll_interval`, sleep only when an iteration did no work, and optionally cap commits per iteration. Count only jobs that are still on the scheduler towards the budget. Fix F5 first: it removes most of the per-launch cost. [controller_throughput.md §2.2](controller_throughput.md#22-interleave-dispatch-with-collection-and-stop-counting-finished-jobs) lists the steps, the invariants that must survive, the tests, and how to check a real run. Any change under `src/` changes `harness_identity()`, so land it between runs ([controller_throughput.md §4](controller_throughput.md#4-rollout-notes)).

**Status** Implemented on branch `bug_fixes` (steps 2.2a-e); not yet checked on a real LSF run.

### F6. Job-level scheduler limits are attributed to the running child without corroboration, and `started` is meaningless for single LSF jobs

**Where** [orchestrator.py:600-606](../src/benchpress_compare/orchestrator.py#L600-L606) (`termination`, `limit`, `started`) and [orchestrator.py:657-658](../src/benchpress_compare/orchestrator.py#L657-L658) (`elif limit and started: RESOURCE_LIMIT`), [orchestrator.py:666-669](../src/benchpress_compare/orchestrator.py#L666-L669) (`not started`), [executors/lsf.py:141](../src/benchpress_compare/executors/lsf.py#L141) (`job.json` written before `bsub`).

**Problem** (code added in `82be945`)

- A shard's `-W` is the *sum* of its children's watchdogs (plus 11 %), so `TERM_RUNLIMIT` on a shard says the shard was slow overall, not that the child running at that moment exceeded its own `wall_seconds`. That child is nonetheless committed as a terminal `RESOURCE_LIMIT` (`wall_limit`): a baseline child is dropped from the score, an evolve child blocks the main score. The same applies to `TERM_MEMLIMIT` when the cgroup charge includes the supervisor and page cache.
- For non-shard LSF jobs (`build`, `freeze-targets`, `validate-import`), `started` is `True` whenever `job.json` exists, and `LSFExecutor.submit` writes it before calling `bsub`, so `job_ended_before_start` can never fire and a job killed while still `PEND` is classified from `termination` alone.

**Suggested fix** Require the child's own telemetry to agree before committing `RESOURCE_LIMIT`: `supervisor.resources.elapsed_seconds >= 0.9 * wall_seconds` for `wall_limit`, `peak_rss_bytes >= 0.9 * memory_bytes` for `memory_limit`; otherwise `INFRA_FAILED`. For single jobs derive "started" from scheduler evidence (`bjobs -o start_time`, or the `Dispatched`/`Starting` lines already parsed by `parse_history`) rather than from the local `job.json`.

### F7. `_process_tree` can crash on a `ProcessLookupError` race

**Where** [executors/local.py:63-72](../src/benchpress_compare/executors/local.py#L63-L72). `os.getpgid(child.pid)` raises `ProcessLookupError` (an `OSError`) when a child exits between `children(recursive=True)` and the call; the handler catches only `psutil.NoSuchProcess`, which is not related to `ProcessLookupError` (verified: `issubclass(psutil.NoSuchProcess, ProcessLookupError)` is `False`).

**Problem** Short-lived descendants are common (hundreds of `rustc` processes during a build task, polled every 0.1 s locally and 10×/s inside shards). The exception escapes `poll()`: inside a shard it kills the supervisor (remaining members become infrastructure casualties; before `82be945` they were caught by F1), locally it ends the orchestrator loop with exit code 2 and `status.json` stuck at `RUNNING`.

**Suggested fix** Catch `(psutil.NoSuchProcess, ProcessLookupError)` around the per-child block, and in `_group_exists` ([local.py:78-85](../src/benchpress_compare/executors/local.py#L78-L85)) treat `PermissionError` as "group exists".

### F8. Local preparation needs at least 20 GiB of *available* RAM regardless of profile

**Where** [preparation.py:276-282](../src/benchpress_compare/preparation.py#L276-L282) (`memory_gb = max(config.worker_memory_gb, 16)` for local builds), [pilot.py:70](../src/benchpress_compare/pilot.py#L70) (budget is 80 % of available memory), [orchestrator.py:534-540](../src/benchpress_compare/orchestrator.py#L534-L540) (`Zero capacity` when a profile exceeds the budget).

**Problem** Even `--profile small` on a laptop or VM with less than 20 GiB free fails preparation with `Zero capacity for observation task-…`. The watchdog is a sampling limit, not a reservation, so there is no reason to refuse to start. `--build-cpus` is configurable but the build memory is not.

**Suggested fix** Add `--build-memory-gb` (default 16) and cap it at the inventory budget with a warning, or exempt `task` specs from the hard capacity check.

### F9. Default `--worker-memory-gb 8` is below the planner's own measured peaks for `BIG_CIRCUITS`

**Where** [planner.py:14-22](../src/benchpress_compare/planner.py#L14-L22) records `bwt_n37` at 9 GB and `square_root_n60` at 8 GB; [config.py:50](../src/benchpress_compare/config.py#L50) defaults to 8 GiB; `prepare()` freezes one profile for every case ([preparation.py:494-498](../src/benchpress_compare/preparation.py#L494-L498)).

**Problem** With defaults, the full profile predictably hits `RESOURCE_LIMIT` on both arms for those inputs, so the baseline fails and the inputs are silently dropped. The per-case profile machinery exists (`observations_for(..., profiles=...)`, `plan["runtime_profiles"]`) but is unused.

**Suggested fix** Give `BIG_CIRCUITS` a larger per-case profile in `prepare()` (for example `max(worker_memory_gb, observed_peak_gb * 1.5)`), or raise the default, or at least document the requirement in the README next to the profile table.

### F10. Preparation tasks inherit the observation watchdog

**Where** [preparation.py:193-198](../src/benchpress_compare/preparation.py#L193-L198): `_task_spec` defaults to `wall_seconds=config.wall_seconds`, which is 60 s for `small` and 900 s for `evolve`.

**Problem** `freeze-fixture` for the largest fixtures (45 MB JSON in `results/full*/fixtures/inputs`, round-tripped through import/export), `freeze-targets` (FakeTorino plus every synthetic width, each self-checked by `import_target`) and `validate-import` can exceed 60 s on a loaded node or a slow shared filesystem, which is then a `RESOURCE_LIMIT` and `PREPARATION_FAILED`.

**Suggested fix** Use a dedicated preparation wall (for example `max(config.wall_seconds, 1800)`) for fixture, target and import tasks.

### F11. Instrumentation failures are counted as compiler failures of the arm

**Where** [worker/adapter.py:101-103](../src/benchpress_compare/worker/adapter.py#L101-L103) (`inspect.signature` on a pyo3 function), [adapter.py:139-141](../src/benchpress_compare/worker/adapter.py#L139-L141) and [starts.py:86-91](../src/benchpress_compare/worker/starts.py#L86-L91) (private `best_subset` ABI called with positional arguments), [worker/__init__.py:213-224](../src/benchpress_compare/worker/__init__.py#L213-L224).

**Problem** Any exception other than `AdapterIncompatible`/`UnsupportedCircuit` raised inside the wrappers (a `TypeError` from a changed `best_subset` signature, a `ValueError` from a missing `__text_signature__`, a hashing error in `structural_signature`) propagates through `manager.run` and is recorded as `COMPILER_FAILED` with the Python exception as reason. That silently counts a harness problem against the arm (and, for baseline, drops the input).

**Suggested fix** Wrap the bodies of `wrap` and `native_start_templates` in `try/except Exception as exc: raise AdapterIncompatible(...) from exc`, keeping the `__function(*args, **kwargs)` call itself outside the wrapper's own `try`.

### F12. LSF single jobs run with the submitting shell's environment

**Where** `LSFExecutor.submit` ([executors/lsf.py:127-196](../src/benchpress_compare/executors/lsf.py#L127-L196)) passes no environment to `bsub`; [worker/__init__.py:49-55](../src/benchpress_compare/worker/__init__.py#L49-L55) sets `PYTHONNOUSERSITE` and pops `PYTHONPATH` only after the interpreter has already built `sys.path`.

**Problem** Only shard children get the sanitised `worker_environment`. The controller, `resolve-dependencies`, `build`, `freeze-targets` and `validate-import` jobs inherit `PYTHONPATH`, user site-packages, `~/.qiskit/settings.conf`, proxies and so on from the launching shell. A developer's `PYTHONPATH=src` would make these jobs import the live coordinator source instead of the pinned harness wheel. The runtime attestation catches a *different dependency inventory* but not a same-version import from another path.

**Suggested fix** Submit with an explicit environment (`bsub -env "none, PATH=…, HOME=…, …"` or wrap the command in `env -i …`), reusing `worker_environment` for workers and `sanitized_environment` for builds. Have `_attest_runtime` also check `benchpress_compare.__file__` is inside the attested environment.

### F13. Export can trip over its own lease heartbeat

**Where** [export.py:116-117](../src/benchpress_compare/export.py#L116-L117) holds `Lease(root / "state/controller.lease")` with the heartbeat enabled and excludes only the lease file; [identity.py:85](../src/benchpress_compare/identity.py#L85) writes through a temp file `.controller.lease.XXXXXX` in the same `state/` directory; [export.py:26-39](../src/benchpress_compare/export.py#L26-L39) and [export.py:96-100](../src/benchpress_compare/export.py#L96-L100) list and re-list the tree.

**Problem** If a heartbeat (every 40 s) lands during `_entries` or between the two inventories, the temporary file is listed and then gone: `lstat` raises `FileNotFoundError` (CLI exit 2) or the final comparison raises "Artifact inventory changed during export". Long exports of a full-profile tree make this likely.

**Suggested fix** Exclude `state/controller.lease*` and `state/.controller.lease.*` (or the whole `state/` lease namespace) from the archive inventory, or take the inventory while holding `lease.fenced()`.

---

## Low

### F14. Store bundles are published unreadable to other users

[identity.py:85](../src/benchpress_compare/identity.py#L85) (`mkstemp`, mode 0600) and [store.py:237](../src/benchpress_compare/store.py#L237) (`mkdtemp`, mode 0700). Observed in `store/`: bundle directories are `drwx------`, `manifest.json` and `COMPLETE` are `-rw-------`. A store "shared across runs" is therefore private to one Unix user. If cross-user reuse is intended, `chmod` the temp file/dir to `0o666 & ~umask` / `0o777 & ~umask` before publishing; otherwise document the limitation.

### F15. Build locator includes the kernel release

[preparation.py:286-295](../src/benchpress_compare/preparation.py#L286-L295) hashes `platform.platform()` (for example `Linux-5.14.0-…el9_8.x86_64-…`). On a heterogeneous cluster, baseline builds are not reused across nodes with different kernels, and a controller restarted mid-build on another node starts a second, duplicate build task. Use `sysconfig.get_platform()` plus the libc version (both already in the attestation).

### F16. Build-task peak RSS ignores children

[tasks.py:97-102](../src/benchpress_compare/tasks.py#L97-L102) uses `RUSAGE_SELF`, so `uv`/`cargo`/`rustc` are excluded. Combine with `RUSAGE_CHILDREN`. Diagnostic only.

### F17. `uv build` failures for the harness wheel lose their stderr and surface as a traceback

[preparation.py:102-109](../src/benchpress_compare/preparation.py#L102-L109): `check=True` with `stderr=PIPE` raises `CalledProcessError`, which the CLI does not map (`except (OSError, ValueError)` at [cli.py:253](../src/benchpress_compare/cli.py#L253)). Catch it and raise `PreparationError` including `exc.stderr`.

### F18. Misleading error text for failed preparation tasks

Any failed task reaches [orchestrator.py:711-712](../src/benchpress_compare/orchestrator.py#L711-L712) ("Worker protocol incompatible: …") before `_tasks` can build its clearer message at [preparation.py:234-241](../src/benchpress_compare/preparation.py#L234-L241). A failing evolve compile is reported as a protocol incompatibility. Reword, or let `_tasks` classify first.

### F19. Adopted local workers lose their exit code after a controller restart

[executors/local.py:182-186](../src/benchpress_compare/executors/local.py#L182-L186) and [local.py:196-201](../src/benchpress_compare/executors/local.py#L196-L201): without a `Popen` handle `return_code` is `None`, which maps to `DONE`. A native crash after a restart becomes `HARNESS_FAILED` (F1) rather than `COMPILER_FAILED`. Treat `None` as unknown, or run workers through a tiny launcher that writes `exit.json`.

### F20. Report reads a diagnostics key the worker never writes

[report.py:154](../src/benchpress_compare/report.py#L154) reads `routing_burden`; the adapter writes `relative_routing_burden` ([adapter.py:252](../src/benchpress_compare/worker/adapter.py#L252)). The column is always null.

### F21. Worker environment does not pin `PYTHONHASHSEED`

[executors/local.py:17-36](../src/benchpress_compare/executors/local.py#L17-L36) omits it (builds set it to `0` at [release.py:41](../src/benchpress_compare/builds/release.py#L41)). Python-side passes that iterate over sets of strings can order differently between processes, so an A/A run can differ for identical seeds. Adding it to `worker_environment` is safe; adding it to the frozen `thread_environment` would change observation identities and invalidate the cache, so do that only with a protocol bump.

### F22. `--group-by circuit` renders an empty Slices table

[report.py:424-426](../src/benchpress_compare/report.py#L424-L426) skips the `input_id` group unconditionally; the data is in `summary.json` only.

### F23. The `small` profile has a single F8 input and no reduced-design escape hatch

`data/profiles.json` lists one F8 input (`Q/large/32`) for `small`; `--allow-missing-families` is rejected for subset profiles at [config.py:71-72](../src/benchpress_compare/config.py#L71-L72). One baseline failure of that input (for example a 60 s watchdog hit on a slow node) yields "No weights could be assigned" and no score at all. Add a second F8 input, or permit the reduced design for subsets with the existing labelling.

### F24. Vestigial code and documentation drift

- `Journal.checkpoint` ([journal.py:88-100](../src/benchpress_compare/journal.py#L88-L100)) is unused; `SUPERSEDED` is read at [orchestrator.py:296](../src/benchpress_compare/orchestrator.py#L296) and [orchestrator.py:815](../src/benchpress_compare/orchestrator.py#L815) but never written; `ObservationState`/`RunState` in `schema.py` are unused.
- The `preflight`/`pilot` phases (seed ranges, `corpus/embedding.py`, the preflight branch at [worker/__init__.py:235-243](../src/benchpress_compare/worker/__init__.py#L235-L243), the export paths at [export.py:154-157](../src/benchpress_compare/export.py#L154-L157)) are never planned; `docs/design/high_level_design.md` still describes preflight eligibility and a pilot that the code does not run.
- `width_band` is duplicated ([weights.py:36](../src/benchpress_compare/weights.py#L36), [corpus/__init__.py:86](../src/benchpress_compare/corpus/__init__.py#L86)); `fsync_directory` is duplicated with different return types ([identity.py:66](../src/benchpress_compare/identity.py#L66), [store.py:35](../src/benchpress_compare/store.py#L35)).
- [worker/adapter.py:249-250](../src/benchpress_compare/worker/adapter.py#L249-L250) is a no-op assignment.

### F25. `worker/__main__.py` bypasses `read_json`

[worker/__main__.py:14](../src/benchpress_compare/worker/__main__.py#L14) uses `json.loads`, so a spec containing `NaN` is accepted where every other reader rejects it.

### F26. `follow_controller` depends on cross-host wall clocks

[controller.py:170](../src/benchpress_compare/controller.py#L170) compares `status.json`'s `updated_at` (compute node) with `submitted_at` (launch host). Clock skew larger than the run duration makes the client never recognise the terminal state and end with "Controller ended without a terminal run state". Compare on the controller token or lease generation instead.

### F27. Report files embed full per-observation diagnostics

[report.py:242-245](../src/benchpress_compare/report.py#L242-L245) copies every observation record minus `artifacts` (pass lists, native call records, 133-entry layout templates). Measured: `summary.json` is 4.5 MB for 264 observations, so about 90 MB for the full profile, plus a comparable `observations.csv`. Keep the rendered fields and reference the bundles for the rest.

### F28. `status()` hashes every frozen fixture on every call

[orchestrator.py:757-772](../src/benchpress_compare/orchestrator.py#L757-L772) calls `verify_plan`, which hashes all `artifact_hashes` ([orchestrator.py:56-59](../src/benchpress_compare/orchestrator.py#L56-L59)): 167 MB of fixtures for the full profile, and `follow_controller` calls `status()` every `poll_seconds`. Cache by `(size, mtime_ns)` for the progress view and keep the full check in `report`/`collect`.

### F29. The baseline staging snapshot is never removed

[preparation.py:154-161](../src/benchpress_compare/preparation.py#L154-L161) leaves `snapshots/baseline-staging` (a full copy of the baseline tree plus its tar) in the results directory after the store copy is published; it is also exported.

### F30. Exports include `uv` caches

[export.py:154-193](../src/benchpress_compare/export.py#L154-L193) archives everything under `results` except build `env/` directories, including `preparation/harness/uv-cache` and `preparation/dependencies/scratch/uv-cache`. Exclude cache directories.

### F31. Stale controller job records are re-queried on every `cancel`

[orchestrator.py:808-812](../src/benchpress_compare/orchestrator.py#L808-L812) cancels every `jobs/controllers/*/job.json`, which `submit_controller` writes once at submission and never updates ([controller.py:146-149](../src/benchpress_compare/controller.py#L146-L149) updates only `jobs/controller.json`). Each old controller costs scheduler queries and, once it has aged out of `bhist`, an "unresolved" `ReconciliationError` even though the live jobs were cancelled.

### F32. Definition-kind gates keep standard names on import

[corpus/ir.py:226-232](../src/benchpress_compare/corpus/ir.py#L226-L232) reconstructs a custom-definition gate as `Gate(name=…)` with its original name. A controlled gate with a non-default `ctrl_state` is exported as kind `definition` named for example `cx`; Qiskit's unrolling decides by name whether a gate is native, so such a gate can be treated as a plain `cx`. No current corpus fixture seems affected, but a name suffix for definition-kind gates would remove the risk.

### F33. Results-directory emptiness check is bypassed on the LSF path

[controller.py:86-100](../src/benchpress_compare/controller.py#L86-L100) writes `request.json` on the launch host before `prepare()` runs, so the "New results directory must be empty" rule at [preparation.py:386-388](../src/benchpress_compare/preparation.py#L386-L388) never applies to LSF runs. Apply the same check in `submit_controller` before writing the request.

---

## Areas checked and found consistent

- Seed derivation and phase ranges (`seeds.py`); case and observation identities (`identity.py`).
- Exact weight allocation, marginal audits and the eight-family invariant (`weights.py`); admission and structural-variant deduplication (`eligibility.py`); ratio-of-means geometric aggregation, zero handling and circuit classification (`aggregate.py`).
- Lease fencing, journal hash chain, atomic bundle publication and verification (`store.py`, `journal.py`, `identity.py`).
- Target snapshot sealing and import/export round trip (`targets.py`); typed IR round trip (`corpus/ir.py`); recursive target legality (`validation/`).
- Build attestation, environment manifests and workspace cleanup (`builds/release.py`); deterministic source snapshots (`builds/snapshot.py`).
- The scheduler-termination classification added in `82be945` is an improvement over the previous behaviour; F6 lists what it still leaves open.
