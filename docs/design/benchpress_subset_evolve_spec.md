# Benchpress evolve subset specification

Prepared 2026-11-10. **Status: proposed subset design; not yet run. The harness needs the changes listed in §7 before it can run this design.**

This specification defines a reduced, fast design for use as the fitness evaluation of automated search frameworks such as [CORAL](https://github.com/Human-Agent-Society/CORAL). It keeps the eight-family structure and weighting policy of the [full specification](benchpress_subset_spec.md), but evaluates **44 inputs on one target class at one optimization level**, and is sized to finish within **40 minutes end to end on the LSF cluster**, including the evolve source build.

The [small subset](benchpress_subset_small_spec.md) is a strict subset of this design: 22 of the 44 inputs, the same target, level and seeds. A candidate can be screened on the small subset first and evaluated here only if it passes.

## 1. Scope and relation to the full specification

The comparison is the same as in the full specification: `baseline/` against `evolve/` through full preset transpilation, measuring final native two-qubit count (N2) and two-qubit depth (D2) separately, with SABRE routing work recorded alongside them. Everything not stated here follows the full specification: input identities and source lookup (§4 there), fixture identity and deduplication, target definitions, transpile settings, metrics, validation, admission and failure handling.

A score on this subset is a **search signal, not a benchmark result**. It covers one of the six target/level configurations and 44 of the 136 main inputs. A candidate change found with it must be confirmed on the full specification (all three targets, L2 and L3, all admitted inputs) before any claim is made. The 92 main inputs left out of this subset act as a natural hold-out set for detecting changes that over-fit the selection.

## 2. Target, level and seeds

| Setting | Value |
| --- | --- |
| Target class | `heavy-hex` only: the complete symmetric heavy-hex graph with the smallest odd `d` such that `(5*d*d - 2*d - 1)/2 >= n_declared`; basis `id, sx, x, rz, cz`; no calibration properties. Identical to the full specification's `heavy-hex` snapshot for the same width. |
| Optimization level | L2 only |
| Comparison seeds | 3 per case, derived exactly as in the full run (`sha256-phase-ranges-v1`, comparison phase, default master seed) |
| Cases | one per input: 44 cases, 264 observations (44 × 3 seeds × 2 arms) |

Heavy-hex is chosen because:

- It is the lattice family of IBM devices, including FakeTorino.
- With maximum degree 3 it gives the largest routing workload of the three targets: historical SWAP counts are roughly 1.5× those on `square`.
- It is sized to each input's width, so placement pressure is comparable across bands; FakeTorino gives small inputs a 133-qubit device.
- It has no calibration data, so N2/D2 changes are not confounded by error-aware post-layout selection.

L2 is the default preset level and uses the same SABRE trial budget as L3 (20/20/20).

Because each input has exactly one case, **case weight equals input weight**.

## 3. Selection rules

The 44 inputs were chosen from the 136 main-table inputs of the full specification by these rules, applied per family:

1. **Keep every band the family supports** in the full pool (S/M/L; F4 and F8 have no S, F7 has no L), so the global band split equals the full design's planning split.
2. **Maximize distinct recipe groups** within each band; prefer the recipes that carry the most weight in the full design.
3. **Strong routing signal on heavy-hex**: every selected input inserted a median of at least 50 SWAPs on heavy-hex L2 in the run-003 baseline (or, where that configuration was not measured, at least 101 on heavy-hex L3 or another L2 target). Threshold-edge inputs from the full specification's risk table (`bv_n19`, `multiply_n13`, `tof_5`, the 9–15-qubit Feynman circuits) are excluded.
4. **Include deep workloads**, so that a search cannot win by improving only shallow circuits: `factor247_n15`, `bwt_n21`, `vqe_uccsd_n28`, `hwb10`, `gf2^32_mult`, both QV100 inputs and Clifford-100, each with about 4,800–137,000 inserted SWAPs.
5. **Runtime limits**: baseline time per compile must not exceed 400 s for inputs in `BIG_CIRCUITS` (each observation is its own LSF job), and 70 s for all other inputs (each case's six observations run sequentially in one job). This excludes:
   - `bwt_n37` (800–1,500 s per compile; `square_root_n60`, which was similar, has since been removed from the full pool);
   - `square_root_n45` (106–152 s per compile, not in `BIG_CIRCUITS`);
   - `ham_ham_JW24` (77–116 s per compile).

   Some inputs that meet the limits are still left out, because a cheaper input already covers their recipe: `hwb11` and `hwb12` (`hwb10` is selected), and `ham_ham_JW-22` and `ham_ham_BK22` (`ham_ham_JW-18` is selected).
6. **Avoid known representation risks** where an equivalent alternative exists: `cc_n32`/`cc_n64` (classical control flow) and `bwt_n37` are not selected; `bwt_n21` (mid-circuit resets) is kept as F6's only deep, non-Hamiltonian workload. Structural variants and §5 screening candidates are never selected.
7. **The small subset is contained in this one**: its 22 inputs were chosen first, and the rest were added to cover the L band and the deep workloads.

## 4. Inventory and weights

Weights use the full specification's formula over the selected inputs, with one case per input:

```text
q[S] = 1/5; q[M] = 3/10; q[L] = 1/2
Z[f]       = sum(q[b] over bands present in family f in this subset)
K[f,b]     = number of recipe groups of family f, band b in this subset
N[f,b,k]   = number of selected inputs in recipe k, family f, band b
W[i] = w[c] = (1/8) * q[b]/Z[f] / K[f,b] / N[f,b,k]
```

Each family receives exactly 1/8. Global band shares are S 7/40 (17.5%), M 57/160 (35.625%) and L 15/32 (46.875%), the same as the full design's planning allocation. The two largest single weights are `Q/large/vqe_uccsd_n28` (7.5%, F7's only M input, as in the full design) and `S/test_QFT_100_transpile` (6.25%).

**Baseline s/compile** is the run-003 baseline median for heavy-hex L2 (single-threaded, LSF `normal` queue). Values marked `*` were not measured on heavy-hex L2. For those, the column shows the largest median among the measured L2 configurations and heavy-hex L3. **Baseline SWAPs** is the median of inserted SWAPs on heavy-hex L2, unless the cell is annotated with its source configuration. `(big)` marks inputs in the harness's `BIG_CIRCUITS` list, whose observations each run as their own job. These numbers are planning evidence, not admission evidence; admission is decided from the subset's own baseline results (§6).

### F1. Fourier, phase estimation and factoring — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs | In small subset |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | :---: |
| `Q/medium/factor247_n15` | 15 | S | `factoring` | 1/160 | 0.625% | 202.9* (big) | 74,938 (L3) | |
| `Q/medium/qf21_n15` | 15 | S | `factoring` | 1/160 | 0.625% | 1.6 | 50 | yes |
| `Q/medium/qft_n18` | 18 | S | `QFT` | 1/80 | 1.25% | 2.0 | 139 | yes |
| `Q/large/qft_n63` | 63 | M | `QFT` | 3/80 | 3.75% | 5.1* | 1,268 (other target) | yes |
| `S/test_QFT_100_transpile` | 100 | L | `QFT` | 1/16 | 6.25% | 5.5 | 2,844 | |

### F2. Register arithmetic — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs | In small subset |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | :---: |
| `Q/medium/multiplier_n15` | 15 | S | `integer-multiplication` | 1/80 | 1.25% | 2.3* | 101 (other target) | yes |
| `F/qcla_com_7.qasm` | 24 | S | `carry-lookahead` | 1/80 | 1.25% | 2.1 | 82 | |
| `F/gf2^10_mult.qasm` | 30 | M | `finite-field-multiplication` | 3/160 | 1.875% | 2.3 | 431 | yes |
| `Q/large/adder_n64` | 64 | M | `ripple-addition` | 3/160 | 1.875% | 3.1 | 230 | yes |
| `F/gf2^32_mult.qasm` | 96 | L | `finite-field-multiplication` | 1/32 | 3.125% | 11.0 | 4,795 | |
| `Q/large/adder_n118` | 118 | L | `ripple-addition` | 1/32 | 3.125% | 3.4 | 561 | |

### F3. Boolean, oracle, hub and parity-check circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs | In small subset |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | :---: |
| `F/hwb10.qasm` | 16 | S | `hidden-weighted-bit` | 1/120 | 0.8333% | 55.6 | 26,153 | |
| `F/ham15-med.qasm` | 17 | S | `ham15-boolean` | 1/120 | 0.8333% | 1.7 | 279 | yes |
| `F/barenco_tof_10.qasm` | 19 | S | `multi-control-toffoli` | 1/120 | 0.8333% | 2.1 | 70 | yes |
| `Q/large/swap_test_n41` | 41 | M | `swap-test-kNN` | 3/80 | 3.75% | 1.9 | 90 | yes |
| `S/test_BV_100_transpile` | 100 | L | `BV` | 1/32 | 3.125% | 2.0* | 211 (L3) | |
| `Q/large/knn_129` | 129 | L | `swap-test-kNN` | 1/32 | 3.125% | 5.1 | 443 | |

### F4. Variational and quantum-learning circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs | In small subset |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | :---: |
| `Q/large/dnn_n33` | 33 | M | `DNN-hub` | 3/256 | 1.172% | 1.9 | 96 | yes |
| `Q/large/qugan_n39` | 39 | M | `QUGAN` | 3/128 | 2.344% | 2.1 | 115 | yes |
| `Q/large/dnn_n51` | 51 | M | `DNN-hub` | 3/256 | 1.172% | 2.1* | 159 (L3) | yes |
| `S/test_circSU2_89_transpile` | 89 | L | `circular-SU2` | 5/128 | 3.906% | 8.1 | 462 | |
| `Q/large/qugan_n111` | 111 | L | `QUGAN` | 5/128 | 3.906% | 3.6 | 556 | |

### F5. Combinatorial optimization — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs | In small subset |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | :---: |
| `H/ham_complbipart-n-10_a-5_b-5` | 10 | S | `quantum-MaxCut-Hamiltonian` | 1/80 | 1.25% | 2.0 | 57 | yes |
| `Q/medium/sat_n11` | 11 | S | `SAT-oracle` | 1/80 | 1.25% | 1.8 | 97 | yes |
| `H/ham_reg-5_n-10_rinst-07` | 30 | M | `Max-k-Cut-Hamiltonian` | 3/80 | 3.75% | 5.6* | 1,467 (L3) | yes |
| `H/ham_gnp-k_5_n-90_rinst-02` | 90 | L | `quantum-MaxCut-Hamiltonian` | 1/48 | 2.083% | 9.2 | 8,232 | |
| `H/ham_tsp_prob-pr76_Ncity-10_enc-unary` | 100 | L | `TSP-Hamiltonian` | 1/48 | 2.083% | 3.2 | 1,530 | |
| `S/test_QAOA_100_transpile` | 100 | L | `QAOA` | 1/48 | 2.083% | 4.7* | 2,578 (L3) | |

### F6. Spin/lattice simulation and quantum walks — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs | In small subset |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | :---: |
| `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-4_U-40_enc-gray_d-8` | 12 | S | `Bose-Hubbard` | 1/80 | 1.25% | 3.8 | 860 | yes |
| `Q/medium/bwt_n21` | 21 | S | `BWT-quantum-walk` | 1/80 | 1.25% | 214.3 (big) | 137,259 | |
| `H/ham_bh_graph-2D-triag-nonpbc-qubitnodes_Lx-3_Ly-5_U-30_enc-gray_d-4` | 28 | M | `Bose-Hubbard` | 3/160 | 1.875% | 3.4 | 771 | yes |
| `H/ham_bh_graph-1D-grid-nonpbc-qubitnodes_Lx-12_U-40_enc-unary_d-4` | 48 | M | `Bose-Hubbard` | 3/160 | 1.875% | 4.9 | 695 | yes |
| `H/ham_graph-2D-triag-pbc-qubitnodes_Lx-13_Ly-13_h-0.1` | 91 | L | `TFIM` | 1/32 | 3.125% | 2.3 | 745 | |
| `H/ham_fh-graph-1D-grid-pbc-qubitnodes_Lx-50_U-2_enc-jw` | 100 | L | `Fermi-Hubbard` | 1/32 | 3.125% | 3.2 | 850 | |

### F7. Electronic and vibrational chemistry — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs | In small subset |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | :---: |
| `Q/small/vqe_uccsd_n8` | 8 | S | `UCCSD` | 1/100 | 1% | 4.1 | 203 | yes |
| `Q/medium/gcm_h6` | 13 | S | `GCM-chemistry` | 1/100 | 1% | 2.7* | 143 (L3) | |
| `H/ham_enc_gray_dvalues_4-4-4-4-4-4-4` | 14 | S | `molecule:all-vib-fccf` | 1/100 | 1% | 3.7 | 628 | yes |
| `H/ham_ham_JW14` | 14 | S | `molecule:Li2` | 1/100 | 1% | 5.0 | 1,096 | yes |
| `H/ham_ham_JW-18` | 18 | S | `molecule:C2` | 1/100 | 1% | 16.6* | 7,854 (other target) | |
| `Q/large/vqe_uccsd_n28` | 28 | M | `UCCSD` | 3/40 | 7.5% | 258.5* (big) | 59,256 (L3) | |

### F8. Random and Clifford circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs | In small subset |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | :---: |
| `Q/large/32` | 32 | M | `quantum-volume` | 3/64 | 4.688% | 4.8 | 1,201 | yes |
| `Q/large/100` | 100 | L | `quantum-volume` | 5/256 | 1.953% | 64.0* | 28,079 (L3) | |
| `S/test_QV_100_transpile` | 100 | L | `quantum-volume` | 5/256 | 1.953% | 53.3 | 28,003 | |
| `S/test_clifford_100_transpile` | 100 | L | `clifford` | 5/128 | 3.906% | 19.4 | 18,190 | |

## 5. Runtime budget on LSF

**Budget: at most 40 minutes end to end** (submission to `COMPLETE`) for a run with a freshly built evolve arm, on the CCC cluster configuration in `lsf-site.toml` (`normal` queue, `max_jobs = 100`).

The estimate is built from run 003, whose 2,072 receipts and journal give measured compile times, preparation-task durations and controller throughput:

| Phase | Measured basis | Estimate |
| --- | --- | ---: |
| Preparation | Dependency resolution 92 s; release build about 450 s per arm (arms build concurrently; the baseline build is reused from the store); import validation about 130 s; fixture freezing about 2 s per fixture in parallel jobs. Run 003 reached the frozen plan in 18.9 min with two fresh builds and 149 fixtures. | 13–19 min |
| Controller overhead | The controller is single-threaded. It submits about 2.6 observations/s and commits about 2.2 observations/s, so roughly 0.85 s per observation, partly overlapped with execution. | 3–4 min |
| Critical-path job | 59 jobs: 41 one-case jobs with six sequential observations each, plus 18 single-observation jobs for the three `BIG_CIRCUITS` inputs. All run concurrently under the 100-job limit. The longest job is the `Q/large/100` case, about 6 × 64 s ≈ 6.4 min; the longest single observation is `vqe_uccsd_n28` at about 260 s. | 6–7 min |
| Polling and dispatch | LSF dispatch took 1–4 s per job in run 003; the controller polls every 20 s. | about 1 min |
| **Total at baseline speed** | | **about 25–31 min** |

The remaining 9–15 minutes absorb an evolve build that compiles more slowly and evolve compile times up to about 1.5× baseline. Total compile work is about 102 CPU-minutes.

After the first run, verified baseline observations are reused from the store (§3 of [benchmark_flow.md](../benchmark_flow.md)), so only the 132 evolve observations are compiled. That cuts collection time roughly in half. Preparation does not change, because every candidate needs its own evolve build.

The budget assumes:

- an uncontended queue;
- the store and results root on a filesystem with free quota;
- a baseline build already in the store.

Queue contention extends the runtime one-for-one with pending time.

Recommended runtime settings:

| Flag | Value | Reason |
| --- | --- | --- |
| `--executor lsf --lsf-config ./lsf-site.toml` | | site configuration above |
| `--seeds` | `3` | |
| `--native-threads` | `1` | matches the timing evidence |
| `--worker-memory-gb` | `8` | largest selected peak is about 2.5 GB (`bwt_n21`); 8 GB keeps all 59 jobs within the 2,000 GB cap |
| `--wall-seconds` | `900` | about 3.5× the slowest baseline compile (`vqe_uccsd_n28`, about 260 s). A candidate that exceeds it becomes an evolve resource failure, and the main score becomes unavailable, which an evolve loop should treat as a reject. |

The runtime profile is part of the baseline cache key. Keep these values fixed for the whole search, or the cached baseline is not reused.

## 6. Admission, scoring and use as a fitness signal

- **Admission** is the full specification's post-collection rule applied to the single case: an input is dropped when any baseline seed of its heavy-hex L2 case fails, is invalid or is missing. Weights are then recomputed over the admitted inputs with the formula in §4. If a family becomes empty, the subset has no score; weight is not redistributed. Admission reads baseline results only, so it is fixed after the first run and identical for every candidate.
- **Scoring** is unchanged: per case, the exact ratio `mean(evolve seeds) / mean(baseline seeds)`, for N2 and D2 separately; overall, the weighted geometric mean of the case ratios. Lower is better.
- **Evolve failures**: an evolve compile failure, invalid output or watchdog timeout where baseline succeeded makes the main score unavailable. The report's partial score excludes those circuits and must not be used as fitness on its own.
- **Fitness**: how the evolve loop combines N2 and D2 into one objective (for example N2 as the objective with a D2 non-regression constraint, or a Pareto front) belongs to the search configuration, not to this specification. The harness reports both separately and never combines them.
- **Seed noise**: three seeds are averaged per case. Changes in the overall ratio smaller than the reported seed spread should not drive selection. Confirm improvements on the full specification.

## 7. Harness support required

The current harness cannot run this design yet:

1. **Target and level selection.** Case expansion hard-codes all three target classes and both levels ([preparation.py:490-502](../../src/benchpress_compare/preparation.py#L490-L502)). It must expand each input to the heavy-hex L2 case only, and freeze only the heavy-hex target snapshots the selected widths need.
2. **Weight audit.** `audit_case_weights` requires all six target/level cases per weighted input ([weights.py:200](../../src/benchpress_compare/weights.py#L200)). A subset design needs a variant that requires exactly one case per input, with case weight equal to input weight, and audits family totals of 1/8.
3. **Input selection and design name.** `--smoke` already filters inputs, but it labels the run `benchpress-smoke-v1` and skips the family requirement. The subset should be a named design, for example `benchpress-evolve-v1`, read from a checked-in manifest that holds the 44 input IDs, the target class, the level and this document's revision. The design name and manifest hash should be recorded in `request.json` and the plan. The eight-family requirement stays on.
4. **Reports** must show the design name and state that the score is a subset search signal, not the full eight-family score.

`BIG_CIRCUITS`, sharding, admission, scoring, metrics and validation need no change.
