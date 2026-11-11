# `small` profile at a glance

A quick screening run: 22 small and medium circuits, heavy-hex only, level 2,
three seeds. It tells you whether a candidate is broken or clearly worse before
you spend half an hour on [`evolve`](evolve.md). It is not a benchmark score.
Design `benchpress-small-v1`; details in the
[specification](../design/benchpress_subset_small_spec.md).

| Setting | Value |
| --- | --- |
| Circuits | 22 (3 per family, 1 for F8) |
| Qubits | 8 to 64 |
| Target / level | `heavy-hex` / 2 |
| Seeds | 3 per circuit |
| Compilations | 132 (22 × 3 seeds × 2 arms) |
| Default watchdog | 60 s per compile |
| Measured on LSF | 16.5 min: 14.5 min preparation, 2 min collection |
| Overlap | all 22 circuits are also in `evolve` |

## Weight distribution

Each family gets 12.5%. Inside a family, medium gets 3/5 and small 2/5. Inside
a band, recipes share equally, then circuits within a recipe.

| Family | Circuits | S | M | Recipes | S weight | M weight | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| F1 Fourier, phase estimation, factoring | 3 | 2 | 1 | 2 | 5% | 7.5% | 12.5% |
| F2 Register arithmetic | 3 | 1 | 2 | 3 | 5% | 7.5% | 12.5% |
| F3 Boolean, oracle, hub, parity check | 3 | 2 | 1 | 3 | 5% | 7.5% | 12.5% |
| F4 Variational, quantum learning | 3 | 0 | 3 | 2 | 0% | 12.5% | 12.5% |
| F5 Combinatorial optimization | 3 | 2 | 1 | 3 | 5% | 7.5% | 12.5% |
| F6 Spin/lattice, quantum walks | 3 | 1 | 2 | 1 | 5% | 7.5% | 12.5% |
| F7 Chemistry | 3 | 3 | 0 | 3 | 12.5% | 0% | 12.5% |
| F8 Random and Clifford | 1 | 0 | 1 | 1 | 0% | 12.5% | 12.5% |
| **Total** | **22** | **11** | **11** | **18** | **37.5%** | **62.5%** | **100%** |

## Circuits

| Family | Circuit | Qubits | Band | Recipe | Weight |
| --- | --- | ---: | :---: | --- | ---: |
| F1 | `Q/medium/qf21_n15` | 15 | S | factoring | 2.5% |
| F1 | `Q/medium/qft_n18` | 18 | S | QFT | 2.5% |
| F1 | `Q/large/qft_n63` | 63 | M | QFT | 7.5% |
| F2 | `Q/medium/multiplier_n15` | 15 | S | integer multiplication | 5% |
| F2 | `F/gf2^10_mult.qasm` | 30 | M | finite-field multiplication | 3.75% |
| F2 | `Q/large/adder_n64` | 64 | M | ripple addition | 3.75% |
| F3 | `F/ham15-med.qasm` | 17 | S | ham15 boolean | 2.5% |
| F3 | `F/barenco_tof_10.qasm` | 19 | S | multi-control Toffoli | 2.5% |
| F3 | `Q/large/swap_test_n41` | 41 | M | swap test / kNN | 7.5% |
| F4 | `Q/large/dnn_n33` | 33 | M | DNN hub | 3.125% |
| F4 | `Q/large/qugan_n39` | 39 | M | QUGAN | 6.25% |
| F4 | `Q/large/dnn_n51` | 51 | M | DNN hub | 3.125% |
| F5 | `H/ham_complbipart-n-10_a-5_b-5` | 10 | S | quantum MaxCut Hamiltonian | 2.5% |
| F5 | `Q/medium/sat_n11` | 11 | S | SAT oracle | 2.5% |
| F5 | `H/ham_reg-5_n-10_rinst-07` | 30 | M | Max-k-Cut Hamiltonian | 7.5% |
| F6 | `H/ham_bh_graph-1D-grid-pbc-qubitnodes_Lx-4_U-40_enc-gray_d-8` | 12 | S | Bose-Hubbard | 5% |
| F6 | `H/ham_bh_graph-2D-triag-nonpbc-qubitnodes_Lx-3_Ly-5_U-30_enc-gray_d-4` | 28 | M | Bose-Hubbard | 3.75% |
| F6 | `H/ham_bh_graph-1D-grid-nonpbc-qubitnodes_Lx-12_U-40_enc-unary_d-4` | 48 | M | Bose-Hubbard | 3.75% |
| F7 | `Q/small/vqe_uccsd_n8` | 8 | S | UCCSD | 4.167% |
| F7 | `H/ham_enc_gray_dvalues_4-4-4-4-4-4-4` | 14 | S | vibrational FCCF | 4.167% |
| F7 | `H/ham_ham_JW14` | 14 | S | Li2 molecule | 4.167% |
| F8 | `Q/large/32` | 32 | M | quantum volume | 12.5% |

No circuit takes more than about 6 s to compile on the baseline. With so few
circuits, a handful dominate:

| Circuit | Weight | Why |
| --- | ---: | --- |
| `Q/large/32` | 12.5% | the only F8 circuit |
| `Q/large/qft_n63` | 7.5% | the only medium circuit in F1 |
| `Q/large/swap_test_n41` | 7.5% | the only medium circuit in F3 |
| `H/ham_reg-5_n-10_rinst-07` | 7.5% | the only medium circuit in F5 |
| `Q/large/qugan_n39` | 6.25% | alone in one of F4's two recipes |

## Not included

| Left out | Why |
| --- | --- |
| Large circuits (65+ qubits) | kept for `evolve` |
| Big circuits, and anything over 10 s per baseline compile | time budget |
| Other F6 recipes | every small or medium F6 circuit that is not a big circuit is Bose-Hubbard |
| More F8 circuits | `Q/large/32` is the only small or medium random circuit in the pool |
| Other targets and levels | heavy-hex has the heaviest routing load; level 2 is the default preset |

Reject a candidate if any evolve compile fails or hits the watchdog, or if N2
or D2 is clearly worse than 1 beyond the seed spread. Otherwise run `evolve`.
Do not tune on this profile.
