# Benchpress subset specification

Prepared 2026-11-09, revised 2026-11-10. **Status: proposed full-pool design; eligibility screening and benchmark collection have not been run for this specification.**

This specification enumerates the **full routing-relevant pool within 8–133 qubits** identified by the supplied studies, rather than the compact 34-circuit shortlist. It defines targets, eight families, every candidate input and its recipe group, width bands, and weights.

The [high-level plan](benchpress_for_sabre_claude_high_level_plan.md) controls the common-cohort policy, L2/L3 restriction and weighting. The [Claude study, Appendices A–B](benchpress_for_sabre_claude.md) supplies the exhaustive historical routing inventory; the [Codex study](benchpress_for_sabre_codex.md) supplies input/target identity details and caveats. The pinned corpus is `4c04b38a53fc231a89f1a6bc9541c1b641ed6dcf`. Local data inspection at that revision verified the listed file paths, HamLib identities and adder aliases.

## 1. Scope and objective

Compare `baseline/` and `evolve/` through full preset transpilation, measuring final native two-qubit count (N2) and two-qubit depth (D2), with observed SABRE routing work reported alongside them. This is a whole-build comparison; it does not isolate SABRE from synthesis and downstream optimization.

The inventory contains **136 distinct main-cohort candidates**: 43 QASMBench inputs, 32 additional Feynman inputs, six Summit inputs, and 55 HamLib inputs. Two more Feynman source names alias the listed QASMBench adders. Every main-table row passes the routing-relevance screen stated in §4: SABRE executed in every historically measured configuration on the three target classes, no measured configuration fell below the 10-SWAP threshold, and the row is not a structural duplicate of another row. Eleven further in-range candidates from the studies fail one of those tests on historical evidence (six below-threshold inputs and five coefficient-only or state-preparation-only variants); §5 lists them with zero main-score weight, together with the target-specific and known-local cases. “Full pool” means the native transpilation inputs covered by the studies, not every unused QASM asset or newly generated size variant.

All numerical input/case weights below are **planning allocations conditional on all 136 candidates being admitted at the listed widths**. They are not an assertion that historical routing relevance satisfies the new screen. Recompute the same declared formula after baseline-only screening and deduplication, then freeze the final manifest before inspecting evolved results. The below-threshold and variant candidates in §5 remain explicit screening candidates; they acquire weight only if the screen admits them, and the formula is then recomputed over the admitted set.

Deep inputs such as BWT, `factor247_n15`, `hwb11/12`, `vqe_uccsd_n28` and `ham_ham_JW24` are included in this full pool. A resource pilot must budget for them; expense alone is not permission to remove them after observing results.

## 2. Targets and optimization levels

Every admitted input uses **all three target classes at both L2 and L3**. Each input therefore contributes six cases.

| Target ID | Frozen target definition | Weight within every input | Overall weight |
| --- | --- | ---: | ---: |
| `square` | Symmetric square grid, side `ceil(sqrt(n_declared))`; basis `id, sx, x, rz, cz`; absent calibration properties | 1/3 | 33⅓% |
| `heavy-hex` | Complete symmetric heavy-hex graph; smallest odd `d` with `(5*d*d - 2*d - 1)/2 >= n_declared`; same basis and absent calibration properties | 1/3 | 33⅓% |
| `fake_torino` | One frozen 133-qubit FakeTorino device snapshot, native CZ, preserving directed instruction support and calibration | 1/3 | 33⅓% |

Use the declared register width to size targets so all wires fit; use verified active logical width for band assignment. Freeze exact edges, capacities and instruction properties. Square/heavy-hex have capacity-specific snapshots; each snapshot receives the sum of its cases' weights, not a separate one-third share. Record occupancy for every input/target pair. FakeTorino and synthetic heavy-hex are distinct configurations.

| Optimization level | Weight within every input/target pair | Overall weight |
| --- | ---: | ---: |
| L2 | 1/2 | 50% |
| L3 | 1/2 | 50% |

L0/L1, linear, all-to-all, tree, torus and extra devices are outside this specification. Use identical frozen inputs and targets across builds, no supplied initial layout, `approximation_degree=1.0`, and the high-level plan's explicit 20/20/20 SABRE trial budgets. Preserve level-specific iterations and record additional deterministic starts. Do not force SABRE solely to make an input qualify.

## 3. Bands, families and weighting

| Band | Active logical qubits, inclusive | Priority within a family supporting all bands |
| --- | ---: | ---: |
| S — small | 8–24 | 20% |
| M — medium | 25–64 | 30% |
| L — large | 65–133 | 50% |

QASMBench source folders named `small`, `medium` and `large` are provenance labels, not these bands. For example, `qft_n18` is S and `adder_n64` is M. Define active logical width as the number of wires used by non-barrier operations in the frozen logical input, including preparation and measurement; unused declared wires do not count toward a band. The inventory widths are the studies' logical register widths; confirm active logical width after freezing fixtures before final band assignment. Preserve declared registers and ancilla semantics. Do not add idle wires or copies to fill a band.

Each semantic family receives **1/8 = 12.5%**. To cover the full pool, this specification makes three explicit extensions to the plan's example taxonomy: F3 includes parity-check/error-correction circuits; F6 includes quantum walks; F7 includes vibrational chemistry. Factoring circuits using phase estimation are assigned to F1. These assignments preserve eight families and are weighting policy, not upstream suite labels. UCCSD and GCM belong only to F7; swap-test/kNN belongs only to F3; QUGAN belongs only to F4.

The BWT, GCM and SECA algorithm identities follow the [QASMBench circuit descriptions](https://github.com/pnnl/QASMBench/blob/master/README.md); the selected fixtures remain pinned to the Benchpress revision above.

The following supported-band allocations describe the listed candidate inventory. Missing bands receive zero; their priority mass is normalized within the same family.

| Family | Scope | Inputs | S share of total | M share of total | L share of total | Total |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| F1 | Fourier, phase estimation and factoring | 6 | 2.5% | 3.75% | 6.25% | 12.5% |
| F2 | Register arithmetic | 31 | 2.5% | 3.75% | 6.25% | 12.5% |
| F3 | Boolean, oracle, hub and parity-check circuits | 27 | 2.5% | 3.75% | 6.25% | 12.5% |
| F4 | Variational and quantum-learning circuits | 6 | 0% | 4.6875% | 7.8125% | 12.5% |
| F5 | Combinatorial optimization | 25 | 2.5% | 3.75% | 6.25% | 12.5% |
| F6 | Spin/lattice simulation and quantum walks | 17 | 2.5% | 3.75% | 6.25% | 12.5% |
| F7 | Electronic and vibrational chemistry | 20 | 5% | 7.5% | 0% | 12.5% |
| F8 | Random and Clifford circuits | 4 | 0% | 4.6875% | 7.8125% | 12.5% |
| **Total** | | **136** | **17.5%** | **35.625%** | **46.875%** | **100%** |

Large has the largest planning allocation. F4 and F8 have no small candidates; F7 has no large candidates. The global shares consequently differ from 20%/30%/50%. Publish the recalculated marginals after screening, and do not claim large-workload priority unless large remains the largest allocation.

For a family `f`, supported band `b`, recipe group `k` and input `i`, define:

```text
q[S] = 1/5; q[M] = 3/10; q[L] = 1/2
Z[f] = sum(q[b] over supported bands in family f)
K[f,b] = number of nonempty recipe groups in that family and band
N[f,b,k] = number of distinct admitted inputs in that recipe group and band

family weight       = 1/8
family-band weight  = (1/8) * q[b]/Z[f]
recipe-band weight  = (1/8) * q[b]/Z[f] / K[f,b]
input weight W[i]   = (1/8) * q[b]/Z[f] / K[f,b] / N[f,b,k]
case weight w[c]    = W[i] / 3 / 2 = W[i]/6
```

Recipe groups divide weight equally within each family/band; inputs divide their recipe's allocation equally. Related sizes share a recipe. Related molecular encodings share the HamLib `ham_problem` group, rather than receiving separate recipe weights for JW/BK/parity. The explicit recipe labels below are part of this specification and must be frozen before comparison.

The next table gives every recipe's weight without relying on input counts: each nonempty recipe in a cell receives the listed fraction of the total score. The circuit tables then divide it by the number of listed inputs in that recipe/band.

| Family | Band | Recipe groups K | Weight of each recipe in this band |
| --- | --- | ---: | ---: |
| F1 | S | 2 | 1/80 (1.25%) |
| F1 | M | 1 | 3/80 (3.75%) |
| F1 | L | 1 | 1/16 (6.25%) |
| F2 | S | 9 | 1/360 (0.277778%) |
| F2 | M | 7 | 3/560 (0.535714%) |
| F2 | L | 3 | 1/48 (2.083333%) |
| F3 | S | 7 | 1/280 (0.357143%) |
| F3 | M | 2 | 3/160 (1.875%) |
| F3 | L | 2 | 1/32 (3.125%) |
| F4 | M | 2 | 3/128 (2.34375%) |
| F4 | L | 2 | 5/128 (3.90625%) |
| F5 | S | 4 | 1/160 (0.625%) |
| F5 | M | 4 | 3/320 (0.9375%) |
| F5 | L | 5 | 1/80 (1.25%) |
| F6 | S | 2 | 1/80 (1.25%) |
| F6 | M | 2 | 3/160 (1.875%) |
| F6 | L | 3 | 1/48 (2.083333%) |
| F7 | S | 15 | 1/300 (0.333333%) |
| F7 | M | 1 | 3/40 (7.5%) |
| F8 | M | 1 | 3/64 (4.6875%) |
| F8 | L | 2 | 5/128 (3.90625%) |

## 4. Complete circuit inventory and individual weights

Every row below is a candidate on `square`, `heavy-hex` and `fake_torino`, at L2 and L3. **Input weight** covers all six cases. **Case weight** is the exact fraction for each individual target/level combination; percentages are rounded only for display.

**Routing-relevance screen for main-table membership.** A row appears below only if, in the Claude study's single-seed probe (Appendices A–B), (a) `SabreLayout` executed in every measured configuration on `square`, `heavy-hex` and FakeTorino at L2 and L3, (b) no measured configuration inserted fewer than 10 SWAPs, and (c) the row is not a structural duplicate of another row (§4, fixture identity). Historical coverage is uneven: QASMBench inputs were measured on `square` and `heavy-hex` only, Feynman and Summit inputs on FakeTorino only, and HamLib inputs on all three. Unmeasured configurations are not evidence of relevance; every row still requires the §6 preflight, including rows without a special caveat. Candidates with a historical sub-threshold reading, and structural variants, are listed in §5 and can enter only through that preflight.

Source-qualified IDs resolve as follows:

| Prefix | Exact source lookup |
| --- | --- |
| `Q/<size>/<id>` | `benchpress/qasm/qasmbench-<size>/<id>/<id>.qasm`, except the four paths below |
| `F/<filename>` | `benchpress/qasm/feynman/<filename>` |
| `S/<test>` | Method of `TestWorkoutDeviceTranspile100Q` in `benchpress/qiskit_gym/device_transpile/test_summit.py` |
| `H/<base_id>` | Unique record in `benchpress/hamiltonian/hamlib/100_representative.json` satisfying `base_id == "ham_" + ham_instance[1:-1]`; preserve its full original `ham_instance` in the manifest |

QASMBench path exceptions, relative to `benchpress/qasm/`:

- `Q/medium/gcm_h6`: `qasmbench-medium/gcm_n13/gcm_h6.qasm`.
- `Q/large/knn_129`: `qasmbench-large/knn_n129/knn_129.qasm`.
- `Q/large/32`: `qasmbench-large/QV_n32/32.qasm`.
- `Q/large/100`: `qasmbench-large/QV_n100/100.qasm`.

The doubled `ham_` in IDs such as `ham_ham_BK12` is intentional. Names alone do not determine width: `qcla_adder_10` uses 36 qubits, `gcm_h6` uses 13, and `hwb12` uses 20.

### F1. Fourier, phase estimation and factoring — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Input weight | Case weight |
| --- | ---: | --- | --- | ---: | ---: |
| `Q/medium/qft_n18` | 18 | S | `QFT` | 1.25% | 1/480 |
| `Q/medium/factor247_n15` | 15 | S | `factoring` | 0.625% | 1/960 |
| `Q/medium/qf21_n15` | 15 | S | `factoring` | 0.625% | 1/960 |
| `Q/large/qft_n29` | 29 | M | `QFT` | 1.875% | 1/320 |
| `Q/large/qft_n63` | 63 | M | `QFT` | 1.875% | 1/320 |
| `S/test_QFT_100_transpile` | 100 | L | `QFT` | 6.25% | 1/96 |

### F2. Register arithmetic — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Input weight | Case weight |
| --- | ---: | --- | --- | ---: | ---: |
| `Q/medium/bigadder_n18` | 18 | S | `bigadder` | 0.277778% | 1/2160 |
| `F/qcla_com_7.qasm` | 24 | S | `carry-lookahead` | 0.277778% | 1/2160 |
| `F/csla_mux_3.qasm` | 15 | S | `carry-sum-select` | 0.277778% | 1/2160 |
| `Q/medium/multiply_n13` | 13 | S | `constant-multiplication` | 0.277778% | 1/2160 |
| `F/gf2^4_mult.qasm` | 12 | S | `finite-field-multiplication` | 0.055556% | 1/10800 |
| `F/gf2^5_mult.qasm` | 15 | S | `finite-field-multiplication` | 0.055556% | 1/10800 |
| `F/gf2^6_mult.qasm` | 18 | S | `finite-field-multiplication` | 0.055556% | 1/10800 |
| `F/gf2^7_mult.qasm` | 21 | S | `finite-field-multiplication` | 0.055556% | 1/10800 |
| `F/gf2^8_mult.qasm` | 24 | S | `finite-field-multiplication` | 0.055556% | 1/10800 |
| `Q/medium/multiplier_n15` | 15 | S | `integer-multiplication` | 0.277778% | 1/2160 |
| `F/mod_mult_55.qasm` | 9 | S | `modular-arithmetic` | 0.138889% | 1/4320 |
| `F/mod_red_21.qasm` | 11 | S | `modular-arithmetic` | 0.138889% | 1/4320 |
| `F/vbe_adder_3.qasm` | 10 | S | `ripple-addition` | 0.069444% | 1/8640 |
| `Q/small/adder_n10` | 10 | S | `ripple-addition` | 0.069444% | 1/8640 |
| `F/rc_adder_6.qasm` | 14 | S | `ripple-addition` | 0.069444% | 1/8640 |
| `F/adder_8.qasm` | 24 | S | `ripple-addition` | 0.069444% | 1/8640 |
| `Q/medium/square_root_n18` | 18 | S | `square-root` | 0.277778% | 1/2160 |
| `F/qcla_mod_7.qasm` | 26 | M | `carry-lookahead` | 0.267857% | 1/2240 |
| `F/qcla_adder_10.qasm` | 36 | M | `carry-lookahead` | 0.267857% | 1/2240 |
| `F/csum_mux_9.qasm` | 30 | M | `carry-sum-select` | 0.535714% | 1/1120 |
| `F/gf2^9_mult.qasm` | 27 | M | `finite-field-multiplication` | 0.178571% | 1/3360 |
| `F/gf2^10_mult.qasm` | 30 | M | `finite-field-multiplication` | 0.178571% | 1/3360 |
| `F/gf2^16_mult.qasm` | 48 | M | `finite-field-multiplication` | 0.178571% | 1/3360 |
| `Q/large/multiplier_n45` | 45 | M | `integer-multiplication` | 0.535714% | 1/1120 |
| `F/mod_adder_1024.qasm` | 28 | M | `modular-arithmetic` | 0.535714% | 1/1120 |
| `Q/large/adder_n28` | 28 | M | `ripple-addition` | 0.267857% | 1/2240 |
| `Q/large/adder_n64` | 64 | M | `ripple-addition` | 0.267857% | 1/2240 |
| `Q/large/square_root_n45` | 45 | M | `square-root` | 0.535714% | 1/1120 |
| `F/gf2^32_mult.qasm` | 96 | L | `finite-field-multiplication` | 2.083333% | 1/288 |
| `Q/large/multiplier_n75` | 75 | L | `integer-multiplication` | 2.083333% | 1/288 |
| `Q/large/adder_n118` | 118 | L | `ripple-addition` | 2.083333% | 1/288 |

### F3. Boolean, oracle, hub and parity-check circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Input weight | Case weight |
| --- | ---: | --- | --- | ---: | ---: |
| `Q/medium/bv_n19` | 19 | S | `BV` | 0.357143% | 1/1680 |
| `F/grover_5.qasm` | 9 | S | `grover-oracle` | 0.357143% | 1/1680 |
| `F/ham15-low.qasm` | 17 | S | `ham15-boolean` | 0.119048% | 1/5040 |
| `F/ham15-med.qasm` | 17 | S | `ham15-boolean` | 0.119048% | 1/5040 |
| `F/ham15-high.qasm` | 20 | S | `ham15-boolean` | 0.119048% | 1/5040 |
| `F/hwb8.qasm` | 12 | S | `hidden-weighted-bit` | 0.089286% | 1/6720 |
| `F/hwb11.qasm` | 15 | S | `hidden-weighted-bit` | 0.089286% | 1/6720 |
| `F/hwb10.qasm` | 16 | S | `hidden-weighted-bit` | 0.089286% | 1/6720 |
| `F/hwb12.qasm` | 20 | S | `hidden-weighted-bit` | 0.089286% | 1/6720 |
| `F/barenco_tof_5.qasm` | 9 | S | `multi-control-toffoli` | 0.089286% | 1/6720 |
| `F/tof_5.qasm` | 9 | S | `multi-control-toffoli` | 0.089286% | 1/6720 |
| `F/barenco_tof_10.qasm` | 19 | S | `multi-control-toffoli` | 0.089286% | 1/6720 |
| `F/tof_10.qasm` | 19 | S | `multi-control-toffoli` | 0.089286% | 1/6720 |
| `Q/medium/seca_n11` | 11 | S | `parity-check-QEC` | 0.178571% | 1/3360 |
| `Q/medium/qec9xz_n17` | 17 | S | `parity-check-QEC` | 0.178571% | 1/3360 |
| `Q/medium/qram_n20` | 20 | S | `qram` | 0.357143% | 1/1680 |
| `Q/large/cc_n32` | 32 | M | `counterfeit-coin` | 0.9375% | 1/640 |
| `Q/large/cc_n64` | 64 | M | `counterfeit-coin` | 0.9375% | 1/640 |
| `Q/medium/swap_test_n25` | 25 | M | `swap-test-kNN` | 0.625% | 1/960 |
| `Q/large/knn_n31` | 31 | M | `swap-test-kNN` | 0.625% | 1/960 |
| `Q/large/swap_test_n41` | 41 | M | `swap-test-kNN` | 0.625% | 1/960 |
| `Q/large/bv_n70` | 70 | L | `BV` | 1.5625% | 1/384 |
| `S/test_BV_100_transpile` | 100 | L | `BV` | 1.5625% | 1/384 |
| `Q/large/knn_n67` | 67 | L | `swap-test-kNN` | 0.78125% | 1/768 |
| `Q/large/swap_test_n83` | 83 | L | `swap-test-kNN` | 0.78125% | 1/768 |
| `Q/large/swap_test_n115` | 115 | L | `swap-test-kNN` | 0.78125% | 1/768 |
| `Q/large/knn_129` | 129 | L | `swap-test-kNN` | 0.78125% | 1/768 |

### F4. Variational and quantum-learning circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Input weight | Case weight |
| --- | ---: | --- | --- | ---: | ---: |
| `Q/large/dnn_n33` | 33 | M | `DNN-hub` | 1.171875% | 1/512 |
| `Q/large/dnn_n51` | 51 | M | `DNN-hub` | 1.171875% | 1/512 |
| `Q/large/qugan_n39` | 39 | M | `QUGAN` | 2.34375% | 1/256 |
| `Q/large/qugan_n71` | 71 | L | `QUGAN` | 1.953125% | 5/1536 |
| `Q/large/qugan_n111` | 111 | L | `QUGAN` | 1.953125% | 5/1536 |
| `S/test_circSU2_89_transpile` | 89 | L | `circular-SU2` | 3.90625% | 5/768 |

### F5. Combinatorial optimization — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Input weight | Case weight |
| --- | ---: | --- | --- | ---: | ---: |
| `H/ham_gnp-k_5_n-24_rinst-04` | 24 | S | `MaxCut-Hamiltonian` | 0.625% | 1/960 |
| `Q/medium/sat_n11` | 11 | S | `SAT-oracle` | 0.625% | 1/960 |
| `H/ham_tsp_rand-002_Ncity-4_enc-stdbinary` | 8 | S | `TSP-Hamiltonian` | 0.3125% | 1/1920 |
| `H/ham_tsp_prob-ulysses22_Ncity-8_enc-stdbinary` | 24 | S | `TSP-Hamiltonian` | 0.3125% | 1/1920 |
| `H/ham_complbipart-n-10_a-5_b-5` | 10 | S | `quantum-MaxCut-Hamiltonian` | 0.625% | 1/960 |
| `H/ham_reg-5_n-10_rinst-07` | 30 | M | `Max-k-Cut-Hamiltonian` | 0.9375% | 1/640 |
| `H/ham_0-uuf100-0810.cnf-40-res` | 40 | M | `SAT-Hamiltonian` | 0.234375% | 1/2560 |
| `H/ham_8-uuf100-0509.cnf-40-cc` | 40 | M | `SAT-Hamiltonian` | 0.234375% | 1/2560 |
| `H/ham_2-uf100-0396.cnf-46-res` | 46 | M | `SAT-Hamiltonian` | 0.234375% | 1/2560 |
| `H/ham_2-uf100-0601.cnf-46-res` | 46 | M | `SAT-Hamiltonian` | 0.234375% | 1/2560 |
| `H/ham_tsp_prob-ts225_Ncity-5_enc-unary` | 25 | M | `TSP-Hamiltonian` | 0.234375% | 1/2560 |
| `H/ham_tsp_prob-lin105_Ncity-7_enc-unary` | 49 | M | `TSP-Hamiltonian` | 0.234375% | 1/2560 |
| `H/ham_tsp_prob-d198_Ncity-8_enc-unary` | 64 | M | `TSP-Hamiltonian` | 0.234375% | 1/2560 |
| `H/ham_tsp_prob-fl417_Ncity-16_enc-stdbinary` | 64 | M | `TSP-Hamiltonian` | 0.234375% | 1/2560 |
| `H/ham_gnp-k_4_n-40_rinst-07` | 40 | M | `quantum-MaxCut-Hamiltonian` | 0.46875% | 1/1280 |
| `H/ham_gnp-k_5_n-60_rinst-19` | 60 | M | `quantum-MaxCut-Hamiltonian` | 0.46875% | 1/1280 |
| `H/ham_dsjc1000.1,n-36,rinst-4` | 72 | L | `Max-k-Cut-Hamiltonian` | 0.416667% | 1/1440 |
| `H/ham_queen13_13,n-28,rinst-0` | 112 | L | `Max-k-Cut-Hamiltonian` | 0.416667% | 1/1440 |
| `H/ham_will199gpia,n-60,rinst-8` | 120 | L | `Max-k-Cut-Hamiltonian` | 0.416667% | 1/1440 |
| `S/test_QAOA_100_transpile` | 100 | L | `QAOA` | 1.25% | 1/480 |
| `H/ham_4-uf100-0246.cnf-70-res` | 70 | L | `SAT-Hamiltonian` | 0.625% | 1/960 |
| `H/ham_4-flat100-7.cnf-90-cc` | 90 | L | `SAT-Hamiltonian` | 0.625% | 1/960 |
| `H/ham_tsp_prob-pr76_Ncity-10_enc-unary` | 100 | L | `TSP-Hamiltonian` | 1.25% | 1/480 |
| `H/ham_gnp-k_5_n-90_rinst-02` | 90 | L | `quantum-MaxCut-Hamiltonian` | 0.625% | 1/960 |
| `H/ham_reg-4_n-90_rinst-07` | 90 | L | `quantum-MaxCut-Hamiltonian` | 0.625% | 1/960 |

### F6. Spin/lattice simulation and quantum walks — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Input weight | Case weight |
| --- | ---: | --- | --- | ---: | ---: |
| `Q/medium/bwt_n21` | 21 | S | `BWT-quantum-walk` | 1.25% | 1/480 |
| `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-4_U-40_enc-gray_d-8` | 12 | S | `Bose-Hubbard` | 1.25% | 1/480 |
| `Q/large/bwt_n37` | 37 | M | `BWT-quantum-walk` | 1.875% | 1/320 |
| `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-14_U-10_enc-stdbinary_d-4` | 28 | M | `Bose-Hubbard` | 0.3125% | 1/1920 |
| `H/ham_bh_graph-2D-triag-nonpbc-qubitnodes_Lx-3_Ly-5_U-30_enc-gray_d-4` | 28 | M | `Bose-Hubbard` | 0.3125% | 1/1920 |
| `H/ham_bh_graph-3D-grid-nonpbc-qubitnodes_Lx-2_Ly-2_Lz-2_U-20_enc-unary_d-4` | 32 | M | `Bose-Hubbard` | 0.3125% | 1/1920 |
| `H/ham_bh_graph-1D-grid-nonpbc-qubitnodes_Lx-12_U-40_enc-unary_d-4` | 48 | M | `Bose-Hubbard` | 0.3125% | 1/1920 |
| `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-24_U-20_enc-stdbinary_d-4` | 48 | M | `Bose-Hubbard` | 0.3125% | 1/1920 |
| `H/ham_bh_graph-2D-triag-pbc-qubitnodes_Lx-5_Ly-5_U-30_enc-unary_d-4` | 60 | M | `Bose-Hubbard` | 0.3125% | 1/1920 |
| `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-10_U-30_enc-unary_d-8` | 80 | L | `Bose-Hubbard` | 0.416667% | 1/1440 |
| `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-46_U-2_enc-gray_d-4` | 92 | L | `Bose-Hubbard` | 0.416667% | 1/1440 |
| `H/ham_bh_graph-2D-grid-nonpbc-qubitnodes_Lx-7_Ly-7_U-70_enc-gray_d-4` | 98 | L | `Bose-Hubbard` | 0.416667% | 1/1440 |
| `H/ham_bh_graph-2D-triag-pbc-qubitnodes_Lx-10_Ly-10_U-30_enc-stdbinary_d-4` | 100 | L | `Bose-Hubbard` | 0.416667% | 1/1440 |
| `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-60_U-10_enc-stdbinary_d-4` | 120 | L | `Bose-Hubbard` | 0.416667% | 1/1440 |
| `H/ham_fh-graph-1D-grid-pbc-qubitnodes_Lx-50_U-2_enc-jw` | 100 | L | `Fermi-Hubbard` | 2.083333% | 1/288 |
| `H/ham_graph-2D-grid-nonpbc-qubitnodes_Lx-5_Ly-15_h-3` | 75 | L | `TFIM` | 1.041667% | 1/576 |
| `H/ham_graph-2D-triag-pbc-qubitnodes_Lx-13_Ly-13_h-0.1` | 91 | L | `TFIM` | 1.041667% | 1/576 |

### F7. Electronic and vibrational chemistry — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Input weight | Case weight |
| --- | ---: | --- | --- | ---: | ---: |
| `Q/medium/gcm_h6` | 13 | S | `GCM-chemistry` | 0.333333% | 1/1800 |
| `Q/small/vqe_uccsd_n8` | 8 | S | `UCCSD` | 0.333333% | 1/1800 |
| `H/ham_ham_JW-10` | 10 | S | `molecule:BH` | 0.333333% | 1/1800 |
| `H/ham_ham_JW-18` | 18 | S | `molecule:C2` | 0.333333% | 1/1800 |
| `H/ham_ham_JW-8` | 8 | S | `molecule:H2` | 0.333333% | 1/1800 |
| `H/ham_ham_parity10` | 10 | S | `molecule:HF` | 0.166667% | 1/3600 |
| `H/ham_ham_BK12` | 12 | S | `molecule:HF` | 0.166667% | 1/3600 |
| `H/ham_ham_JW14` | 14 | S | `molecule:Li2` | 0.333333% | 1/1800 |
| `H/ham_ham_parity-14` | 14 | S | `molecule:N2` | 0.166667% | 1/3600 |
| `H/ham_ham_JW-22` | 22 | S | `molecule:N2` | 0.166667% | 1/3600 |
| `H/ham_ham_BK-14` | 14 | S | `molecule:NH` | 0.166667% | 1/3600 |
| `H/ham_ham_JW-14` | 14 | S | `molecule:NH` | 0.166667% | 1/3600 |
| `H/ham_ham_JW24` | 24 | S | `molecule:Na2` | 0.333333% | 1/1800 |
| `H/ham_ham_BK22` | 22 | S | `molecule:O3` | 0.333333% | 1/1800 |
| `H/ham_ham_JW12` | 12 | S | `molecule:OH` | 0.166667% | 1/3600 |
| `H/ham_ham_parity12` | 12 | S | `molecule:OH` | 0.166667% | 1/3600 |
| `H/ham_enc_gray_dvalues_4-4-4-4-4-4-4` | 14 | S | `molecule:all-vib-fccf` | 0.333333% | 1/1800 |
| `H/ham_enc_unary_dvalues_4-4-4` | 12 | S | `molecule:all-vib-hco` | 0.333333% | 1/1800 |
| `H/ham_enc_gray_dvalues_8-8-8` | 9 | S | `molecule:all-vib-o3` | 0.333333% | 1/1800 |
| `Q/large/vqe_uccsd_n28` | 28 | M | `UCCSD` | 7.5% | 1/80 |

Attribution caveat for Pauli-evolution inputs: in the historical probe the final N2 of `ham_ham_JW12`, `ham_ham_parity12` and `ham_enc_gray_dvalues_8-8-8` was *lower* than their pre-layout two-qubit count despite hundreds of inserted SWAPs, because post-routing optimization compresses the synthesized Pauli ladders. For this family, read the recorded SWAP counts alongside N2/D2; N2 alone under-reports routing cost. `Q/large/vqe_uccsd_n28` alone carries 7.5% of the total score, the largest single-input weight in the design; this follows from F7 having no large-band candidate and is disclosed here rather than adjusted.

### F8. Random and Clifford circuits — 12.5%

| Exact input ID | Qubits | Band | Recipe group | Input weight | Case weight |
| --- | ---: | --- | --- | ---: | ---: |
| `Q/large/32` | 32 | M | `quantum-volume` | 4.6875% | 1/128 |
| `S/test_clifford_100_transpile` | 100 | L | `clifford` | 3.90625% | 5/768 |
| `Q/large/100` | 100 | L | `quantum-volume` | 1.953125% | 5/1536 |
| `S/test_QV_100_transpile` | 100 | L | `quantum-volume` | 1.953125% | 5/1536 |

### Fixture identity and deduplication

- `F/adder.qasm` aliases `Q/small/adder_n10`; `F/bigadder.qasm` aliases `Q/medium/bigadder_n18`. Their QASM text is identical after newline normalization. Count each logical input once at the canonical QASMBench row's weight; source-byte hashes can differ because of line endings.
- Abstract and device HamLib test nodes loading the same record are one logical input. Deduplicate before expanding to the six target/level cases.
- **Structural duplicates.** Two inputs whose frozen pre-layout two-qubit gate sequences are identical (same ordered two-qubit operations on the same wires, ignoring single-qubit gates and rotation angles) present SABRE with the same layout and routing problem and are one routing workload. One is the canonical fixture; the other is a *structural variant* with zero main-score weight, retained in the supplemental manifest so angle-dependent post-routing differences stay observable. Verification against the pinned corpus found five such pairs, all with identical historical SWAP counts at both levels on every measured target:
  - `ham_ham_JW-10` (BH) and `ham_ham_JW10` (OH): identical ordered Pauli strings, 276 terms, coefficients differ. Canonical `ham_ham_JW-10`.
  - `ham_tsp_prob-pr76_Ncity-10_enc-unary` and `ham_tsp_prob-st70_Ncity-10_enc-unary`: identical ordered Pauli strings, 1,001 terms, coefficients differ. Canonical `pr76`.
  - `ham_tsp_prob-fl417_Ncity-16_enc-stdbinary` and `ham_tsp_prob-kroD100_Ncity-16_enc-stdbinary`: identical ordered Pauli strings, 3,841 terms, coefficients differ. Canonical `fl417`.
  - `Q/medium/knn_n25` and `Q/medium/swap_test_n25`: the kNN file adds 24 single-qubit `ry` state-preparation rotations to the same 12 `cswap`, two `h` and one measurement. Canonical `swap_test_n25`.
  - `Q/large/knn_n41` and `Q/large/swap_test_n41`: same relation with 40 `ry` and 20 `cswap`. Canonical `swap_test_n41`.
  No other pair of the 63 in-range routing-relevant HamLib records shares an ordered Pauli-string sequence. Similar gate counts or interaction graphs alone still do not establish duplication; the frozen pre-layout sequence does. Re-run this check on the frozen fixtures before the final manifest.
- Summit QV100 uses `QuantumVolume(100, 100, seed=12345)`; QASMBench QV100 loads its stored QASM. Keep the input representations explicit and share one `quantum-volume` recipe. Their historical pre-layout workloads differ (14,725 versus 5,000 two-qubit blocks), so they are expected to remain distinct; deduplicate only if frozen fixture identity actually matches.
- Summit SU2 uses `EfficientSU2(89, reps=3, entanglement="circular")`. Preserve its parameter state or bind one fixed meaningful parameter set for both builds before screening. Construction-suite `reps=4` is a different fixture.
- Summit QFT, QAOA and Clifford load `qft/qft_N100.qasm`, `qaoa/qaoa_barabasi_albert_N100_3reps.qasm`, and `clifford/clifford_100_12345.qasm`, respectively. BV uses `bv_all_ones(100)`.
- HamLib wraps each ordered operator in `PauliEvolutionGate(time=1)`. Preserve term order, coefficients and synthesis settings. Its JSON data hash at the inspected revision is `c4b460e790f4c2ddcd4ff0220bcdf65d52193155c281fad3e5cdafb4d5c2516c`.

## 5. Target-specific coverage and exclusions — main weight 0

These inputs cannot join the common cohort under their documented local embeddings. Retain useful target-specific coverage as separately reported cases; their weight in the main score is exactly zero. Unmeasured target relevance must be screened, not inferred from the family label.

| Input | Family / band | Target-specific disposition | Main weight |
| --- | --- | --- | ---: |
| `S/test_square_heisenberg_100_transpile` | F6 / L (100) | Matched 10×10 square is local; FakeTorino historically routes; screen synthetic heavy-hex separately | 0 |
| `H/ham_graph-1D-grid-pbc-qubitnodes_Lx-16_h-2` | F6 / S (16) | Local on square and synthetic heavy-hex; FakeTorino historically routes | 0 |
| `H/ham_graph-1D-grid-pbc-qubitnodes_Lx-26_h-6` | F6 / M (26) | Local on square and synthetic heavy-hex; FakeTorino historically routes | 0 |
| `Q/small/dnn_n8` | F4 / S (8) | Local on square; synthetic heavy-hex historically routes; FakeTorino unmeasured | 0 |
| `S/test_circSU2_100_transpile` | F4 / L (100) | Perfect FakeTorino embedding; synthetic pairings cannot rescue common-cohort eligibility | 0 |
| `Q/medium/dnn_n16` | F4 / S (16) | Local on both synthetic targets; any device-specific result is supplemental | 0 |
| `Q/large/ising_n98` | F6 / L (98) | Known embeddable chain; heavy-hex's historical VF2 search failure does not make required routing nonzero | 0 |
| `S/test_BVlike_simplification_transpile` | F3 / L by declared width (100) | Zero two-qubit workload after initialization; excluded | 0 |

Two inputs listed in the 2026-11-09 revision were removed from the pool on 2026-11-10 and carry no weight: `Q/large/square_root_n60` (F2 / M, `square-root`) and `H/ham_bh_graph-2D-triag-pbc-qubitnodes_Lx-3_Ly-22_U-70_enc-unary_d-4` (F6 / L, `Bose-Hubbard`). They are not screening candidates. Under the §3 formula their allocations pass to `Q/large/square_root_n45` and to the five remaining large-band `Bose-Hubbard` inputs; no family, band or recipe share changes.

The next table lists in-range candidates that the studies mark routing-relevant but that fail the §4 main-table screen on historical evidence. They are screening candidates only. A below-threshold candidate joins the main cohort only if the §6 preflight yields a median of at least 10 SWAPs in all six configurations; a structural variant never joins while its canonical fixture is admitted. Counts are single-seed SWAPs at L2/L3.

| Input | Family / band | Reason for zero planning weight | Main weight |
| --- | --- | --- | ---: |
| `Q/small/qpe_n9` | F1 / S (9) | Square 9/10: L2 below threshold; heavy-hex 13/13; FakeTorino unmeasured | 0 |
| `Q/medium/bv_n14` | F3 / S (14) | Square 6/6 below threshold; heavy-hex 12/12 | 0 |
| `Q/medium/cc_n12` | F3 / S (12) | Square 8/6 below threshold; heavy-hex 10/10; also carries 25 classically conditioned gates | 0 |
| `Q/large/bv_n30` | F3 / M (30) | Square 10/9: L3 below threshold; heavy-hex 19/16 | 0 |
| `H/ham_7-uf20-0384.cnf-8-res` | F5 / S (8) | Square 8/8 below threshold; heavy-hex 14/14; FakeTorino 14/15 | 0 |
| `H/ham_mu_x_prime_enc_stdbinary_dvalues_8-8-8-8-8-8-8-8-8-4-4-4-4-4-4` | F7 / S (11) | Square 4/4, heavy-hex 6/6, FakeTorino 6/6: below threshold everywhere | 0 |
| `Q/medium/knn_n25` | F3 / M (25) | Structural variant of `Q/medium/swap_test_n25` (single-qubit state preparation only) | 0 |
| `Q/large/knn_n41` | F3 / M (41) | Structural variant of `Q/large/swap_test_n41` | 0 |
| `H/ham_ham_JW10` | F7 / S (10) | Coefficient-only variant of `H/ham_ham_JW-10` | 0 |
| `H/ham_tsp_prob-st70_Ncity-10_enc-unary` | F5 / L (100) | Coefficient-only variant of `H/ham_tsp_prob-pr76_Ncity-10_enc-unary` | 0 |
| `H/ham_tsp_prob-kroD100_Ncity-16_enc-stdbinary` | F5 / M (64) | Coefficient-only variant of `H/ham_tsp_prob-fl417_Ncity-16_enc-stdbinary` | 0 |

Other verified-local chains, zero/two-qubit-only workloads, construct/manipulate tests, `*transpiled*.qasm`, inputs with fewer than eight active qubits, and declared registers exceeding 133 are excluded with weight zero. The 133 limit applies to logical registers; synthetic targets may have more physical qubits. No wider two-target scaling extension is included here.

The following main-table candidates have specific eligibility or validation risks. Their displayed allocations remain conditional; historical counts are single-seed values, not the required three-seed medians.

| Candidate | Historical issue / required check |
| --- | --- |
| `Q/medium/bv_n19`, `Q/medium/multiply_n13` | Square counts sit exactly at the threshold (10/10 and 10/11); a three-seed median may fall below it |
| `F/tof_5.qasm` | FakeTorino count 10/10 is exactly at the threshold; square (9 physical qubits) and heavy-hex are unmeasured |
| `F/vbe_adder_3.qasm`, `F/mod_mult_55.qasm`, `F/barenco_tof_5.qasm`, `F/rc_adder_6.qasm`, `F/csla_mux_3.qasm` | 9–15-qubit Feynman circuits with FakeTorino counts of 16–31 and no synthetic-target measurement; tight 9–16-qubit square targets may route below threshold |
| `Q/small/adder_n10`, `Q/medium/qec9xz_n17` | Low amplitude on square (12/12 and 12/11); FakeTorino unmeasured for `qec9xz_n17` |
| `S/test_circSU2_89_transpile` | Low routing amplitude (25/25 on FakeTorino). Both synthetic targets are bipartite, so the odd ring forces SABRE to run, but the 10×10 square may need well under 10 SWAPs; synthetic configurations are unmeasured |
| `Q/large/cc_n32`, `Q/large/cc_n64` | Contain 65 and 129 classically conditioned gates (`if(c0==…)`) after a mid-circuit measurement of the ancilla; the OpenQASM 2 loader produces control-flow operations. Require recursive two-qubit depth and control-flow-aware target legality before admission |
| `Q/medium/bwt_n21`, `Q/large/bwt_n37` | Contain 9,207 and 28,415 mid-circuit `reset` instructions. Require supported reset representation and metrics; the target must support each reset on its assigned physical qubit. Semantic equivalence is not checked |
| `Q/medium/qec9xz_n17`, `Q/medium/seca_n11` | Mid-circuit measurements: `qec9xz_n17` measures its eight syndrome wires and continues on other wires; `seca_n11` reuses measured wires (`q[9]`, `q[0]`) in later gates. Require measurement-aware metrics and native measurement support; semantic equivalence is not checked |

## 6. Admission, freeze and weight audit

1. Freeze corpus revision, logical fixtures, target snapshots, both builds and semantic/recipe labels. Verify active widths, declared-register capacity and fixture aliases. No new width variants are implicit in this list.
2. Run the baseline-only eligibility screen with **three independent preflight seeds** on every candidate × target × L2/L3. Require SABRE execution in all three seeds and a median of **at least 10 inserted routing SWAPs in each of the six configurations**. Derive that diagnostic from the SWAP count delta at each native routing boundary under `sabre-native-counts-v3`; it is not a routing-correctness certificate. Record relative routing burden as well. Instrument integrated routing in `SabreLayout`, not only standalone `SabreSwap`.
3. Exclude verified zero-routing embeddings even if VF2 misses them. Mark uncertain embeddings as uncertain. Require supported measurement/control-flow representations, metrics, and recursive native target legality under `target-legality-v4` before admission. Layout validation, routing replay, and semantic equivalence checks are not performed. A failed compile or timeout is unresolved eligibility, not evidence that a circuit is local.
4. Admit an input to the main cohort only if all six configurations qualify. Preserve useful individual target pairings, weak cases and exclusion reasons in the zero-weight coverage manifest. Do not use different input mixtures for different targets or levels.
5. Recompute `S[f]`, `Z[f]`, `K[f,b]`, `N[f,b,k]` and exact weights over the admitted distinct inputs. Freeze both the main 20:30:50 weights and an equal-band sensitivity allocation using `q[b]=1/3` over the same supported bands. If any family is empty, the eight-family score is unavailable; do not redistribute its weight silently.
6. Audit the exact marginals: total 1; each family 1/8; each target 1/3; each level 1/2; each family/target/level 1/48. Publish supported bands and actual global band shares. Missing observations after freeze never change membership or weights.

With all 136 candidates admitted, the plan would contain **816 cases**, **2,448 baseline preflight compilations**, and **4,896 main compilations** for two builds × three matched comparison seeds. These are conditional planning counts, excluding pilots, retries, supplemental coverage, and the preflight of the eleven §5 screening candidates (66 cases, 198 compilations) that must also run before the manifest is frozen. Actual admitted counts require the screen above.

Comparison seeds contribute equally to their case's arithmetic mean; they create no additional workload weight. For each metric separately, compute the evolved/baseline ratio of those means, then the weighted geometric mean over cases. Do not combine N2 and D2 into a single score. Retain an admitted evolved result even if it bypasses SABRE or needs no SWAPs. Missing, invalid or non-positive-ratio observations prevent a full logarithmic score and must not be silently dropped or reweighted.

The final collection manifest must store `input_id`, aliases, full source identity and fixture hash, active/declared width, family, recipe, band, target class/snapshot/hash, level, eligibility evidence, comparison seeds, all weight factors and exact case weight. This Markdown fixes the proposed inventory and allocation policy; it does not claim that this manifest or benchmark results have already been collected.
