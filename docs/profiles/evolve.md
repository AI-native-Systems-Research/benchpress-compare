# `evolve` profile at a glance

The fitness signal for a search loop such as CORAL: 44 circuits across all
three size bands, heavy-hex only, level 2, three seeds. Same families and band
split as `full`, but one target and one level, done in under 40 minutes. It is a
search signal, not a benchmark result: confirm on [`full`](full.md). Design
`benchpress-evolve-v1`; details in the
[specification](../design/benchpress_subset_evolve_spec.md).

| Setting | Value |
| --- | --- |
| Circuits | 44 (4 to 6 per family) |
| Qubits | 8 to 129 |
| Target / level | `heavy-hex` / 2 |
| Seeds | 3 per circuit |
| Compilations | 264 (44 × 3 seeds × 2 arms) |
| Default watchdog | 900 s per compile |
| Measured on LSF | 27 min: 19 min preparation, 8 min collection |
| Overlap | contains all 22 `small` circuits; 44 of the 136 weighted `full` circuits |

## Weight distribution

Each family gets 12.5%. Inside a family, bands are weighted 20:30:50 (S:M:L)
over the bands the family has. Inside a band, recipes share equally, then
circuits within a recipe. The band split is exactly the one `full` uses.

| Family | Circuits | S | M | L | Recipes | S weight | M weight | L weight | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| F1 Fourier, phase estimation, factoring | 5 | 3 | 1 | 1 | 2 | 2.5% | 3.75% | 6.25% | 12.5% |
| F2 Register arithmetic | 6 | 2 | 2 | 2 | 4 | 2.5% | 3.75% | 6.25% | 12.5% |
| F3 Boolean, oracle, hub, parity check | 6 | 3 | 1 | 2 | 5 | 2.5% | 3.75% | 6.25% | 12.5% |
| F4 Variational, quantum learning | 5 | 0 | 3 | 2 | 3 | 0% | 4.6875% | 7.8125% | 12.5% |
| F5 Combinatorial optimization | 6 | 2 | 1 | 3 | 5 | 2.5% | 3.75% | 6.25% | 12.5% |
| F6 Spin/lattice, quantum walks | 6 | 2 | 2 | 2 | 4 | 2.5% | 3.75% | 6.25% | 12.5% |
| F7 Chemistry | 6 | 5 | 1 | 0 | 5 | 5% | 7.5% | 0% | 12.5% |
| F8 Random and Clifford | 4 | 0 | 1 | 3 | 2 | 0% | 4.6875% | 7.8125% | 12.5% |
| **Total** | **44** | **17** | **12** | **15** | **30** | **17.5%** | **35.625%** | **46.875%** | **100%** |

| Heaviest circuits | Weight | Why |
| --- | ---: | --- |
| `Q/large/vqe_uccsd_n28` | 7.5% | F7's only medium circuit, and F7 has no large band |
| `S/test_QFT_100_transpile` | 6.25% | F1's only large circuit |
| `Q/large/32` | 4.69% | F8's only medium circuit |
| `S/test_circSU2_89_transpile` | 3.91% | alone in its F4 large recipe |
| `Q/large/qugan_n111` | 3.91% | alone in its F4 large recipe |
| `S/test_clifford_100_transpile` | 3.91% | alone in its F8 large recipe |

## Circuits

"In small" marks the 22 circuits shared with the `small` profile.

| Family | Circuit | Qubits | Band | Recipe | Weight | In small |
| --- | --- | ---: | :---: | --- | ---: | :---: |
| F1 | `Q/medium/factor247_n15` | 15 | S | factoring | 0.625% | |
| F1 | `Q/medium/qf21_n15` | 15 | S | factoring | 0.625% | yes |
| F1 | `Q/medium/qft_n18` | 18 | S | QFT | 1.25% | yes |
| F1 | `Q/large/qft_n63` | 63 | M | QFT | 3.75% | yes |
| F1 | `S/test_QFT_100_transpile` | 100 | L | QFT | 6.25% | |
| F2 | `Q/medium/multiplier_n15` | 15 | S | integer multiplication | 1.25% | yes |
| F2 | `F/qcla_com_7.qasm` | 24 | S | carry-lookahead | 1.25% | |
| F2 | `F/gf2^10_mult.qasm` | 30 | M | finite-field multiplication | 1.875% | yes |
| F2 | `Q/large/adder_n64` | 64 | M | ripple addition | 1.875% | yes |
| F2 | `F/gf2^32_mult.qasm` | 96 | L | finite-field multiplication | 3.125% | |
| F2 | `Q/large/adder_n118` | 118 | L | ripple addition | 3.125% | |
| F3 | `F/hwb10.qasm` | 16 | S | hidden weighted bit | 0.833% | |
| F3 | `F/ham15-med.qasm` | 17 | S | ham15 boolean | 0.833% | yes |
| F3 | `F/barenco_tof_10.qasm` | 19 | S | multi-control Toffoli | 0.833% | yes |
| F3 | `Q/large/swap_test_n41` | 41 | M | swap test / kNN | 3.75% | yes |
| F3 | `S/test_BV_100_transpile` | 100 | L | Bernstein-Vazirani | 3.125% | |
| F3 | `Q/large/knn_129` | 129 | L | swap test / kNN | 3.125% | |
| F4 | `Q/large/dnn_n33` | 33 | M | DNN hub | 1.172% | yes |
| F4 | `Q/large/qugan_n39` | 39 | M | QUGAN | 2.344% | yes |
| F4 | `Q/large/dnn_n51` | 51 | M | DNN hub | 1.172% | yes |
| F4 | `S/test_circSU2_89_transpile` | 89 | L | circular SU2 | 3.906% | |
| F4 | `Q/large/qugan_n111` | 111 | L | QUGAN | 3.906% | |
| F5 | `H/ham_complbipart-n-10_a-5_b-5` | 10 | S | quantum MaxCut Hamiltonian | 1.25% | yes |
| F5 | `Q/medium/sat_n11` | 11 | S | SAT oracle | 1.25% | yes |
| F5 | `H/ham_reg-5_n-10_rinst-07` | 30 | M | Max-k-Cut Hamiltonian | 3.75% | yes |
| F5 | `H/ham_gnp-k_5_n-90_rinst-02` | 90 | L | quantum MaxCut Hamiltonian | 2.083% | |
| F5 | `H/ham_tsp_prob-pr76_Ncity-10_enc-unary` | 100 | L | TSP Hamiltonian | 2.083% | |
| F5 | `S/test_QAOA_100_transpile` | 100 | L | QAOA | 2.083% | |
| F6 | `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-4_U-40_enc-gray_d-8` | 12 | S | Bose-Hubbard | 1.25% | yes |
| F6 | `Q/medium/bwt_n21` | 21 | S | BWT quantum walk | 1.25% | |
| F6 | `H/ham_bh_graph-2D-triag-nonpbc-qubitnodes_Lx-3_Ly-5_U-30_enc-gray_d-4` | 28 | M | Bose-Hubbard | 1.875% | yes |
| F6 | `H/ham_bh_graph-1D-grid-nonpbc-qubitnodes_Lx-12_U-40_enc-unary_d-4` | 48 | M | Bose-Hubbard | 1.875% | yes |
| F6 | `H/ham_graph-2D-triag-pbc-qubitnodes_Lx-13_Ly-13_h-0.1` | 91 | L | TFIM | 3.125% | |
| F6 | `H/ham_fh-graph-1D-grid-pbc-qubitnodes_Lx-50_U-2_enc-jw` | 100 | L | Fermi-Hubbard | 3.125% | |
| F7 | `Q/small/vqe_uccsd_n8` | 8 | S | UCCSD | 1% | yes |
| F7 | `Q/medium/gcm_h6` | 13 | S | GCM chemistry | 1% | |
| F7 | `H/ham_enc_gray_dvalues_4-4-4-4-4-4-4` | 14 | S | vibrational FCCF | 1% | yes |
| F7 | `H/ham_ham_JW14` | 14 | S | Li2 molecule | 1% | yes |
| F7 | `H/ham_ham_JW-18` | 18 | S | C2 molecule | 1% | |
| F7 | `Q/large/vqe_uccsd_n28` | 28 | M | UCCSD | 7.5% | |
| F8 | `Q/large/32` | 32 | M | quantum volume | 4.688% | yes |
| F8 | `Q/large/100` | 100 | L | quantum volume | 1.953% | |
| F8 | `S/test_QV_100_transpile` | 100 | L | quantum volume | 1.953% | |
| F8 | `S/test_clifford_100_transpile` | 100 | L | Clifford | 3.906% | |

## Slow circuits

Baseline median on heavy-hex at level 2, single thread. Everything not listed
compiles in under 10 s. The three big circuits run every compile as its own LSF
job; all other circuits run one job per circuit (6 compiles).

| Circuit | Baseline compile | Note |
| --- | ---: | --- |
| `Q/large/vqe_uccsd_n28` | about 260 s | big circuit |
| `Q/medium/bwt_n21` | about 215 s | big circuit, 9,207 mid-circuit resets |
| `Q/medium/factor247_n15` | about 200 s | big circuit |
| `Q/large/100` | about 64 s | longest job: 6 compiles, about 6.5 min |
| `F/hwb10.qasm` | about 56 s | |
| `S/test_QV_100_transpile` | about 53 s | |
| `S/test_clifford_100_transpile` | about 19 s | |
| `H/ham_ham_JW-18` | about 17 s | |
| `F/gf2^32_mult.qasm` | about 11 s | |

## Not included

Every selected circuit inserted at least 50 SWAPs on heavy-hex at level 2 in
the reference run, and deep workloads were kept on purpose so a search cannot
win on shallow circuits alone.

| Left out | Why |
| --- | --- |
| `Q/large/bwt_n37` | 800 to 1,500 s per compile |
| `Q/large/square_root_n45`, `H/ham_ham_JW24` | 77 to 152 s per compile, and not big circuits, so they would hold up a shared job |
| `F/hwb11.qasm`, `F/hwb12.qasm` | `hwb10` already covers the recipe |
| `H/ham_ham_JW-22`, `H/ham_ham_BK22` | `ham_ham_JW-18` already covers a large molecule |
| `Q/large/cc_n32`, `Q/large/cc_n64` | classical control flow; avoided where an alternative exists |
| `bv_n19`, `multiply_n13`, `tof_5`, 9–15-qubit Feynman circuits | routing load at the edge of the admission threshold |
| 92 other weighted `full` circuits | hold-out set for detecting over-fitting |

N2 and D2 are reported separately; combining them is the search's job. An
evolve failure where baseline succeeded makes the main score unavailable, which
a search should treat as a reject. Keep `--wall-seconds`, worker memory and
thread count fixed for the whole search so the cached baseline is reused.
