# Benchpress Compare

`benchpress-compare` prepares reproducible comparisons of two Qiskit source trees.
It records release builds, frozen fixtures and targets, baseline admission,
matched seeds, observations, and independent native two-qubit count (N2) and
two-qubit depth (D2) reports. The coordinator and offline reports do not import
Qiskit; each measurement uses the selected build's isolated worker environment.

**Implementation status:** this repository contains the package, local and LSF
execution adapters, durable storage, scientific calculations, reports, and tests.
The frozen validation contract (`target-legality-v4`) requires verified native
target legality for every accepted observation. Layout validation, routing replay,
and semantic equivalence checks are not performed. SABRE activity and native-boundary
SWAP count deltas remain admission diagnostics under `sabre-native-counts-v3`.
Accepted results establish target compatibility, not behavioral equivalence with
the input circuit.
See
[validation coverage](docs/validation_coverage.md) and the
[implementation plan](docs/design/implementation_plan.md).

## Environment

The coordinator uses Python **3.12.12** and pinned dependencies in `uv.lock`.
A local `.venv` is created with uv:

```bash
uv sync --locked
source .venv/bin/activate
benchpress-compare --help
```

To install the separately pinned Qiskit/provider integration dependencies and run
the tests:

```bash
uv sync --locked --group integration
uv run --locked --group integration pytest
uv run --locked ruff check src tests
```

The integration environment is for compatibility tests and fixture/provider
inspection. It is not a substitute for building the supplied Qiskit sources.
Real source builds also need the Rust and native compiler toolchain required by
those source versions, plus access to their common locked dependencies.

## Prepare and run

Use explicit source directories containing `pyproject.toml`, a baseline store,
and a distinct results directory. Store/results paths must not overlap each other
or the source trees. An A/A comparison may supply the same source folder for both
arms; the arms still execute independent observations.

Provide a local Benchpress mirror at revision
`4c04b38a53fc231a89f1a6bc9541c1b641ed6dcf`. The checked-in
[catalogue](data/candidates.json) pins the input assets and includes 138 main-table
candidates, 11 additional screening candidates, aliases, and separate coverage.

```bash
uv run benchpress-compare prepare \
  --baseline /sources/qiskit-baseline \
  --evolve /sources/qiskit-evolve \
  --store /benchmarks/store \
  --results-root /benchmarks/results/change-001 \
  --corpus /sources/benchpress \
  --executor local --seeds 3 --workers auto

uv run benchpress-compare run --results-root /benchmarks/results/change-001
```

`run` with all four paths prepares and collects in one invocation. `prepare`
stops at final plan publication when admission, pilot, and freeze gates pass.
Target legality must pass before a normal full-corpus freeze;
diagnostic preparation artifacts remain available for failures. `--accept-unresolved` records
an explicit conditional cohort decision, but does not make unsupported validation
pass or create missing families. A smoke subset selected by repeated
`--smoke INPUT_ID` arguments is labelled separately and cannot claim the standard
eight-family score.

Use `--native-threads`, `--cpu-budget`, `--memory-gb`, `--worker-memory-gb`,
`--wall-seconds`, `--build-cpus`, and `--workers` to set local budgets. If a
family has no admitted input, the standard eight-family design cannot freeze;
`--allow-missing-families` selects the explicitly named reduced design instead
of redistributing weight. Both arms use the same
frozen observation profile. A zero-capacity allocation is an error. Successful
baseline evidence can be reused only when build, input, target, seed, phase,
protocols, and runtime identities agree.

The full presets explicitly use `qubits_initially_zero=False` in both arms,
matching the fixtures' arbitrary logical-input contract. Compiler-added ancillas
remain zero initialized. This setting is frozen into case and cache identities.

## Resume, report, and export

```bash
uv run benchpress-compare status --results-root /benchmarks/results/change-001
uv run benchpress-compare resume --results-root /benchmarks/results/change-001
uv run benchpress-compare report --results-root /benchmarks/results/change-001 \
  --family F2 --band L --level 3
uv run benchpress-compare cancel --results-root /benchmarks/results/change-001
uv run benchpress-compare export --results-root /benchmarks/results/change-001 \
  --output /benchmarks/change-001.tar
```

Continuation uses saved snapshots and identities. Editing a live source directory
does not update an existing comparison. Successful observations are immutable;
resume reconciles prior attempts before submitting replacements. Ordinary resume
does not reopen explicitly cancelled work; `resume --retry-cancelled` records
that decision. `cancel` acts only on processes/jobs recorded for the run.

Export requires the controller to be stopped. Archives include verified result
artifacts, baseline references, wheels and dependency locks. Installed virtual
environments are omitted because their absolute paths cannot be relocated;
`export.json` records how to recreate them. Offline reports need no environments.

Report filters support circuit, target class or snapshot, level, family, band,
and recipe. Repeated values within one dimension form a union; different dimensions
intersect. Reports are written under `results-root/reports/`:

- `summary.json`: authoritative model, exact case ratios, slices and coverage.
- `report.md`: readable scores, counts, exclusions and provenance.
- `cases.csv`: exact means/ratios, raw seeds and descriptive spread.
- `observations.csv`: individual accepted outcomes and diagnostics.

Each case uses the ratio of arithmetic seed means, then slices use a weighted
geometric mean. N2 and D2 stay separate. Missing seeds and invalid or zero ratios
block the affected score without redistributing weight. Better/same/worse counts
count each canonical circuit once using exact rational comparisons; incomplete
circuits are unclassifiable. Seed variation is descriptive, without a significance
claim or a regression pass/fail threshold.

## LSF

Copy [site.example.toml](docs/site.example.toml), set the queue and site memory
semantics (`LSF_UNIT_FOR_LIMITS` decides whether `-M`/`rusage[mem=]` are MB or
GB), and keep the package, sources/snapshots, store, and results on storage
visible to workers. Preparation tasks and preflight observations are sharded
into sequential jobs; comparison jobs are packed to `target_job_seconds`. The
controller polls its jobs with one `bjobs` listing per cycle and falls back to
`bhist -l` text history, matched by unique job name. Use an execution-platform coordinator environment; a macOS
wheel cannot run on Linux workers.

```bash
uv run benchpress-compare prepare \
  --baseline /shared/sources/qiskit-baseline \
  --evolve /shared/sources/qiskit-evolve \
  --store /shared/benchmarks/store \
  --results-root /shared/benchmarks/results/change-001 \
  --corpus /shared/sources/benchpress \
  --executor lsf --lsf-config /shared/benchmarks/site.toml
```

Submission returns the controller job identity. `--wait` follows it; disconnecting
the following client does not stop the controller. Builds and worker tasks use
LSF resources. Frozen case bundles execute sequentially in fresh subprocesses
with per-observation watchdogs and receipts. Recovery retains successful children
and retries only unfinished observations. Unknown submissions and scheduler outages require reconciliation,
not speculative duplicate jobs. Fake-scheduler tests exercise this behavior;
the target site's accounting output, memory semantics, and filesystem fencing
still require real-site verification before accepting cluster results.

Exit codes are `0` success/submission accepted, `2` configuration or integrity
error, `3` preparation failure, `4` collection failures, `5` reconciliation needed,
and `130` interruption. Submission success does not imply completed measurements.

## Acceptance evidence

The test suite covers deterministic seeds, conditional catalogue allocations,
metric dependencies and dynamic branches, recursive target legality, source/store
integrity, scheduler recovery, and exact scoring/report edge cases. The full
conditional catalogue reproduces all 138 planning case fractions, including the
7.5% allocation to `vqe_uccsd_n28`; those are planning weights, not admission data.

Recorded checks against Qiskit 2.5.2 and the pinned Benchpress revision include:

- [Inventory audit](data/inventory_audit.json): all 149 candidate assets loaded,
  round-tripped through the typed logical format, and matched their declared and
  active widths. This checks representation and asset integrity, not admission.
- [Historical dynamic validation spike](data/validation_feasibility.json): `cc_n12`,
  `qec9xz_n17`, and `seca_n11` compiled on the square target at L2 with verified
  native metrics, target/layout checks, and routing replay. Their required
  end-to-end semantic checks were unsupported under that earlier contract.
  These historical observations are not acceptance evidence or reusable results
  for the current protocol.
- [Worker integration tests](tests/test_worker.py): bounded real A/A observations
  execute both arms independently, preserve committed work on resume, and reuse
  only baseline observations in a subsequent run.

The adapted Summit fixture recipes retain Benchpress provenance and its
[Apache 2.0 license](data/LICENSE-BENCHPRESS.txt), also included in the package.

Outstanding gates are verification against the actual supplied source versions, real-site LSF
and shared-filesystem checks, full baseline admission/pilot, and a real frozen
cohort A/A rehearsal with independent evolve observations. Test success alone is
not evidence that those gates passed.
