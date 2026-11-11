# Benchpress small subset specification

Prepared 2026-11-10. **Status: proposed subset design; not yet run. The harness needs the changes listed in §7 of the [evolve subset specification](benchpress_subset_evolve_spec.md) before it can run this design.**

This specification defines the smallest useful design: **three small or medium inputs per family, one target class, L2 only, three seeds, and no big circuits**. It is meant as a quick check before the [evolve subset](benchpress_subset_evolve_spec.md), for example as the first stage of an evolve loop that rejects broken or clearly worse candidates cheaply. Its benchmark collection should finish within **10 minutes on the LSF cluster**; §4 explains why that budget cannot include preparation.

Everything not stated here follows the [full specification](benchpress_subset_spec.md) and the evolve subset specification: input identities, fixture identity, target definition, transpile settings, metrics, validation, admission, scoring and failure handling.

## 1. Relation to the other designs

- **Contained in the evolve subset.** All 22 inputs are among the evolve subset's 44, with the same target, level and seeds. So these 22 cases are exactly a subset of the evolve subset's cases.
- **A quick check, not a score.** It has no large-band inputs, and F8 has one input. A result here says whether a candidate is worth the 40-minute evolve evaluation, not whether it improves the benchmark.

## 2. Target, level and seeds

| Setting | Value |
| --- | --- |
| Target class | `heavy-hex` only, as defined in the full specification and the evolve subset (§2 there) |
| Optimization level | L2 only |
| Comparison seeds | 3 per case, derived exactly as in the full run |
| Cases | one per input: 22 cases, 132 observations (22 × 3 seeds × 2 arms) |

Case weight equals input weight.

## 3. Selection and weights

Selection rules, per family:

1. **Bands S and M only** (8–64 active qubits). The large band is left to the evolve subset.
2. **No big circuits**: nothing in the harness's `BIG_CIRCUITS` list (`factor247_n15`, `bwt_n21`, `vqe_uccsd_n28`, `hwb11`, `hwb12`, `bwt_n37`). Also excluded is any input whose baseline compile exceeds 10 s on heavy-hex L2, for example `hwb8`, `hwb10`, `square_root_n45` and the larger HamLib molecules such as `ham_ham_JW-18`.
3. **Three inputs per family**, using as many distinct recipe groups as the family's eligible S/M inputs allow, and at least one M input where the family has M candidates. F1 (`QFT`, `factoring`) and F4 (`DNN-hub`, `QUGAN`) have only two eligible recipes, so two of their three inputs share one.
4. **Strong routing signal**: a median of at least 50 inserted SWAPs on heavy-hex L2 in the run-003 baseline (or at least 101 in the closest measured configuration where heavy-hex L2 was not measured). Threshold-edge inputs from the full specification's risk table are excluded.

Two families cannot meet rule 3 in full:

- **F8** has only one S/M input in the whole pool (`Q/large/32`). It is the family's only input and carries the full 12.5% family weight, the largest single weight in this design.
- **F6** has no S/M input outside `BIG_CIRCUITS` other than Bose-Hubbard Hamiltonians, so all three F6 inputs share the `Bose-Hubbard` recipe, at three different lattices and encodings.

Weights use the evolve subset's formula (§4 there) over these 22 inputs. Each family receives exactly 1/8. Global band shares are S 3/8 (37.5%) and M 5/8 (62.5%); with no L band, they differ from the full design's split, as expected.

Column meanings are as in the evolve subset: **Baseline s/compile** is the run-003 heavy-hex L2 median, or, where marked `*`, the largest median among the measured L2 configurations and heavy-hex L3. **Baseline SWAPs** is the heavy-hex L2 median unless the cell is annotated with its source configuration.

### F1. Fourier, phase estimation and factoring — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `Q/medium/qf21_n15` | 15 | S | `factoring` | 1/40 | 2.5% | 1.6 | 50 |
| `Q/medium/qft_n18` | 18 | S | `QFT` | 1/40 | 2.5% | 2.0 | 139 |
| `Q/large/qft_n63` | 63 | M | `QFT` | 3/40 | 7.5% | 5.1* | 1,268 (other target) |

### F2. Register arithmetic — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `Q/medium/multiplier_n15` | 15 | S | `integer-multiplication` | 1/20 | 5% | 2.3* | 101 (other target) |
| `F/gf2^10_mult.qasm` | 30 | M | `finite-field-multiplication` | 3/80 | 3.75% | 2.3 | 431 |
| `Q/large/adder_n64` | 64 | M | `ripple-addition` | 3/80 | 3.75% | 3.1 | 230 |

### F3. Boolean, oracle, hub and parity-check circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `F/ham15-med.qasm` | 17 | S | `ham15-boolean` | 1/40 | 2.5% | 1.7 | 279 |
| `F/barenco_tof_10.qasm` | 19 | S | `multi-control-toffoli` | 1/40 | 2.5% | 2.1 | 70 |
| `Q/large/swap_test_n41` | 41 | M | `swap-test-kNN` | 3/40 | 7.5% | 1.9 | 90 |

### F4. Variational and quantum-learning circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `Q/large/dnn_n33` | 33 | M | `DNN-hub` | 1/32 | 3.125% | 1.9 | 96 |
| `Q/large/qugan_n39` | 39 | M | `QUGAN` | 1/16 | 6.25% | 2.1 | 115 |
| `Q/large/dnn_n51` | 51 | M | `DNN-hub` | 1/32 | 3.125% | 2.1* | 159 (L3) |

### F5. Combinatorial optimization — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `H/ham_complbipart-n-10_a-5_b-5` | 10 | S | `quantum-MaxCut-Hamiltonian` | 1/40 | 2.5% | 2.0 | 57 |
| `Q/medium/sat_n11` | 11 | S | `SAT-oracle` | 1/40 | 2.5% | 1.8 | 97 |
| `H/ham_reg-5_n-10_rinst-07` | 30 | M | `Max-k-Cut-Hamiltonian` | 3/40 | 7.5% | 5.6* | 1,467 (L3) |

### F6. Spin/lattice simulation and quantum walks — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-4_U-40_enc-gray_d-8` | 12 | S | `Bose-Hubbard` | 1/20 | 5% | 3.8 | 860 |
| `H/ham_bh_graph-2D-triag-nonpbc-qubitnodes_Lx-3_Ly-5_U-30_enc-gray_d-4` | 28 | M | `Bose-Hubbard` | 3/80 | 3.75% | 3.4 | 771 |
| `H/ham_bh_graph-1D-grid-nonpbc-qubitnodes_Lx-12_U-40_enc-unary_d-4` | 48 | M | `Bose-Hubbard` | 3/80 | 3.75% | 4.9 | 695 |

### F7. Electronic and vibrational chemistry — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `Q/small/vqe_uccsd_n8` | 8 | S | `UCCSD` | 1/24 | 4.167% | 4.1 | 203 |
| `H/ham_enc_gray_dvalues_4-4-4-4-4-4-4` | 14 | S | `molecule:all-vib-fccf` | 1/24 | 4.167% | 3.7 | 628 |
| `H/ham_ham_JW14` | 14 | S | `molecule:Li2` | 1/24 | 4.167% | 5.0 | 1,096 |

### F8. Random and Clifford circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Weight | Weight % | Baseline s/compile | Baseline SWAPs |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `Q/large/32` | 32 | M | `quantum-volume` | 1/8 | 12.5% | 4.8 | 1,201 |

## 4. Runtime budget on LSF

**Budget: at most 10 minutes of collection** (from the frozen plan to `COMPLETE`).

A 10-minute end-to-end budget is not achievable with a freshly built evolve arm. Preparation alone takes about 13–19 minutes, independent of how many circuits are in the subset, because:

- dependency resolution takes about 92 s;
- the release build of the evolve source tree takes about 450 s;
- import validation takes about 2 min;
- LSF dispatch and controller polling add time between preparation steps.

Expect about **16–23 minutes end to end**. Getting under 10 minutes end to end would need a faster build path (for example, an incremental developer build of the evolve tree). The harness does not support that, and it would weaken the build-isolation guarantees.

Collection estimate from run-003 measurements:

| Phase | Basis | Estimate |
| --- | --- | ---: |
| Controller overhead | about 0.85 s per observation for submission and commit, 132 observations | about 2 min |
| Critical-path job | 22 jobs, one case each (six sequential observations), all running concurrently. The slowest compile is about 5.6 s, so the longest job is about 34 s. | under 1 min |
| Polling and dispatch | 20 s poll interval; 1–4 s dispatch per job | about 1 min |
| **Collection at baseline speed** | | **about 3–4 min** |

Total compile work is about 7 CPU-minutes. The remaining margin covers evolve compile times several times slower than baseline. After the first run, verified baseline observations are reused from the store, so only the 66 evolve observations are compiled.

Recommended runtime settings:

| Flag | Value | Reason |
| --- | --- | --- |
| `--seeds` | `3` | |
| `--native-threads` | `1` | matches the timing evidence |
| `--worker-memory-gb` | `8` | the largest selected input used about 0.2 GB in run 003 |
| `--wall-seconds` | `60` | about 10× the slowest baseline compile. Even if every observation in a job hit the watchdog, the job would end within about 6 minutes, keeping a broken candidate inside the budget. |

`--wall-seconds` is part of the runtime profile and therefore of the baseline cache key. With 60 s here and 900 s in the evolve subset, the two designs do not share cached baseline observations. Each design computes its baseline once and then reuses it for the whole search. Keep each design's flags fixed across the search.

## 5. Use in an evolve loop

The admission and scoring rules are those of the evolve subset (§6 there). A practical cascade:

1. Run the small subset. Reject the candidate if any evolve observation fails (compile failure, invalid output or watchdog timeout), or if the N2 or D2 ratio is clearly worse than 1 beyond the reported seed spread.
2. Run the evolve subset only for candidates that pass, and use its score as the fitness signal.
3. Confirm any final candidate on the full specification.

Do not tune a candidate on the small subset alone: it has no L band, and F6 and F8 are each covered by a single recipe.
