# Implementation review and cluster acceptance

Review date: 2026-11-10. The implementation plan remains the comparison contract.
The full source-build comparison is **not yet complete**; the attempt log below
distinguishes diagnostic checks from accepted benchmark observations.

## Corrections from review

- Restored required semantic validation. Protocol v2 allowed unsupported
  semantics to pass; v3 requires verified semantics and prevents reuse of those
  permissive observations. Missing validation cannot change the scored cohort.
- Added independently checked compositional certificates across compiler pass
  boundaries: exact bounded local operators, routing replay, composed layout
  maps, ordered Pauli product-formula expansion, commuting gate fusion, and
  diagonal gates followed by complete-support measurement. Every unsupported
  transformation remains a failed required gate. These are not full-width state
  simulations or proofs of exact evolution under a noncommuting Hamiltonian sum.
- Closed false acceptance paths where custom gates used names such as `swap`,
  `barrier`, or `cz`; typed semantics now govern validation and metrics. Preserved
  conditions, classical overwrite order, and independent measurement ordering.
- Made `qubits_initially_zero=False` explicit in both full presets and their
  identity, matching the fixtures' arbitrary-logical-input contract. Captured
  effective Pauli term-order settings even when Qiskit's serializer omits them.
- Added source-attested native SABRE start templates, including dense,
  ascending/descending and applicable ring starts, separately from the twenty
  random starts and supplied partial starts.
- Fixed LSF cancellation to verify the unique job name before using a saved job
  ID, include controllers and shard submissions, and continue cancelling known
  jobs when another submission is uncertain. Duplicate scheduler names now
  require reconciliation; persisted terminal evidence survives history expiry.
- Pinned coordinator identity before preparation and verified cached harness
  wheel contents against the running code, preventing mixed implementations
  during queued launches or interrupted preparation.
- Fixed export discovery and verification of nested evolved builds, excluding
  their nonrelocatable virtual environments.

Independent review of seed separation, exact weights, ratio-of-means scoring,
geometric aggregation, missing-data behavior, and circuit counts found no
additional mathematical defect.

## Recorded validation

- Before the first full submission: **287 tests passed**; Ruff and
  `git diff --check` passed. Integration tests use the separately pinned Qiskit
  2.5.2 environment, so they do not replace checks of the supplied 2.6 sources.
- The local corpus audit verified all 149 candidates and pinned assets at
  `4c04b38a53fc231a89f1a6bc9541c1b641ed6dcf`.
- Normal-queue job **1486968** passed all thirteen shared-storage and
  process-cleanup tests on a compute node.
- Jobs **1487400–1487401** verified compute-client exclusion from a live
  login-host lease, fifty simultaneous same-key publications, checksums and
  directory fsync. Evidence is under `.probe/site-validation/`.
- Strict semantic integration probes verified `cc_n12`, `qec9xz_n17`,
  `seca_n11`, and QV32. Broader probes exposed commuting-rewrite gaps; their
  remaining jobs were stopped, and the failing `dnn_n33` and `mod_mult_55`
  boundaries subsequently passed independent certificates. Probe artifacts are
  diagnostic only and never enter preflight, pilot, comparison, or baseline cache.

## Requested full comparison

```bash
.venv/bin/python -m benchpress_compare run \
  --baseline /u/ophirazu/projects/qiskit-baseline \
  --evolve /u/ophirazu/projects/qiskit-wait-time \
  --store ./store --results-root ./results \
  --corpus /u/ophirazu/projects/benchpress \
  --executor lsf --lsf-config ./lsf-site.toml \
  --seeds 3 --worker-memory-gb 32 --wall-seconds 7200
```

The site configuration selects `normal`, uses GB for LSF memory semantics,
and limits dispatch to 100 jobs, 120 CPUs and 2,000 GB. Release builds preserve
the supplied optimized Cargo profiles and use eight CPUs and 32 GB each.
Screening must establish all 2,682 baseline observations before cohort freeze.

| Attempt | Controller | Outcome |
| --- | --- | --- |
| 1 | 1489599 | Preparation failed in dependency-resolution job 1489609: an `ibm-platform-services` source build lacked `pkg_resources` in its isolated build environment. No benchmark observations collected; no LSF jobs remained after failure. |

Acceptance still requires verified actual source builds, completed baseline
admission and pilot, a frozen valid cohort, all planned paired comparison
observations, and complete independently verified N2/D2 reports. Submission or
diagnostic-test success does not satisfy those requirements.
