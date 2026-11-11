# `full` profile at a glance

The benchmark itself: every routing-relevant Benchpress circuit from 8 to 133
qubits, on three targets at two optimization levels, three seeds each. It is
the only profile whose score supports a claim; [`small`](small.md) and
[`evolve`](evolve.md) only decide whether a `full` run is worth it. Design
`benchpress-eight-family-v1`; details in the
[specification](../design/benchpress_subset_spec.md).

| Setting | Value |
| --- | --- |
| Circuits compiled | 147: 136 weighted, 6 marginal-routing candidates, 5 near-duplicates |
| Qubits | 8 to 129 |
| Targets | `square`, `heavy-hex`, `fake_torino` (1/3 each) |
| Optimization levels | 2 and 3 (1/2 each) |
| Cases | 6 per circuit, 882 in total |
| Seeds | 3 per case |
| Compilations | 5,292 (147 × 6 cases × 3 seeds × 2 arms) |
| LSF jobs | 1,062 |
| Default watchdog | 3,600 s per compile |
| Measured on LSF | 62 min: 20 min preparation, 43 min collection (an earlier run took 4.8 h) |
| Overlap | contains all 44 `evolve` and all 22 `small` circuits |

## Targets

| Target | Definition | Share |
| --- | --- | ---: |
| `square` | square grid with side ceil(sqrt(n)), sized per circuit; basis id, sx, x, rz, cz; no calibration data | 1/3 |
| `heavy-hex` | smallest complete heavy-hex graph that fits, sized per circuit; same basis; no calibration data | 1/3 |
| `fake_torino` | one frozen 133-qubit FakeTorino snapshot with its directed couplings and calibration data | 1/3 |

Levels 2 and 3 split each target in half, so every family/target/level cell is
exactly 1/48 of the score.

## Weight distribution

Each family gets 12.5%. Inside a family, bands are weighted 20:30:50 (S:M:L)
over the bands the family has. Inside a band, recipes share equally, then
circuits within a recipe, then the circuit's 6 cases. Numbers assume all 136
weighted circuits are admitted; bands are S 8–24 qubits, M 25–64, L 65–133.

| Family | Circuits | S | M | L | Recipes | S weight | M weight | L weight | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| F1 Fourier, phase estimation, factoring | 6 | 3 | 2 | 1 | 2 | 2.5% | 3.75% | 6.25% | 12.5% |
| F2 Register arithmetic | 31 | 17 | 11 | 3 | 9 | 2.5% | 3.75% | 6.25% | 12.5% |
| F3 Boolean, oracle, hub, parity check | 27 | 16 | 5 | 6 | 9 | 2.5% | 3.75% | 6.25% | 12.5% |
| F4 Variational, quantum learning | 6 | 0 | 3 | 3 | 3 | 0% | 4.6875% | 7.8125% | 12.5% |
| F5 Combinatorial optimization | 25 | 5 | 11 | 9 | 7 | 2.5% | 3.75% | 6.25% | 12.5% |
| F6 Spin/lattice, quantum walks | 17 | 2 | 7 | 8 | 4 | 2.5% | 3.75% | 6.25% | 12.5% |
| F7 Chemistry | 20 | 19 | 1 | 0 | 15 | 5% | 7.5% | 0% | 12.5% |
| F8 Random and Clifford | 4 | 0 | 1 | 3 | 2 | 0% | 4.6875% | 7.8125% | 12.5% |
| **Total** | **136** | **62** | **41** | **33** | **51** | **17.5%** | **35.625%** | **46.875%** | **100%** |

| Source | Circuits | ID prefix |
| --- | ---: | --- |
| HamLib Hamiltonians (Pauli evolution, time 1) | 55 | `H/` |
| QASMBench | 43 | `Q/` |
| Feynman | 32 | `F/` |
| Benchpress 100-qubit device tests ("Summit") | 6 | `S/` |

## Recipes, band by band

Inside a band every recipe gets the same share, however many circuits it has,
so a one-circuit recipe makes that circuit heavy and a five-circuit recipe
spreads the same share thinly.

| Family | Band | Recipes (circuits in each) | Each recipe | Each circuit |
| --- | :---: | --- | ---: | ---: |
| F1 | S | QFT (1), factoring (2) | 1.25% | 0.625% to 1.25% |
| F1 | M | QFT (2) | 3.75% | 1.875% |
| F1 | L | QFT (1) | 6.25% | 6.25% |
| F2 | S | bigadder (1), carry-lookahead (1), carry-sum-select (1), constant multiplication (1), finite-field multiplication (5), integer multiplication (1), modular arithmetic (2), ripple addition (4), square root (1) | 0.278% | 0.056% to 0.278% |
| F2 | M | carry-lookahead (2), carry-sum-select (1), finite-field multiplication (3), integer multiplication (1), modular arithmetic (1), ripple addition (2), square root (1) | 0.536% | 0.179% to 0.536% |
| F2 | L | finite-field multiplication (1), integer multiplication (1), ripple addition (1) | 2.083% | 2.083% |
| F3 | S | Bernstein-Vazirani (1), Grover oracle (1), ham15 boolean (3), hidden weighted bit (4), multi-control Toffoli (4), parity-check QEC (2), QRAM (1) | 0.357% | 0.089% to 0.357% |
| F3 | M | counterfeit coin (2), swap test / kNN (3) | 1.875% | 0.625% to 0.9375% |
| F3 | L | Bernstein-Vazirani (2), swap test / kNN (4) | 3.125% | 0.781% to 1.5625% |
| F4 | M | DNN hub (2), QUGAN (1) | 2.344% | 1.172% to 2.344% |
| F4 | L | QUGAN (2), circular SU2 (1) | 3.906% | 1.953% to 3.906% |
| F5 | S | MaxCut Hamiltonian (1), SAT oracle (1), TSP Hamiltonian (2), quantum MaxCut Hamiltonian (1) | 0.625% | 0.3125% to 0.625% |
| F5 | M | Max-k-Cut Hamiltonian (1), SAT Hamiltonian (4), TSP Hamiltonian (4), quantum MaxCut Hamiltonian (2) | 0.9375% | 0.234% to 0.9375% |
| F5 | L | Max-k-Cut Hamiltonian (3), QAOA (1), SAT Hamiltonian (2), TSP Hamiltonian (1), quantum MaxCut Hamiltonian (2) | 1.25% | 0.417% to 1.25% |
| F6 | S | BWT quantum walk (1), Bose-Hubbard (1) | 1.25% | 1.25% |
| F6 | M | BWT quantum walk (1), Bose-Hubbard (6) | 1.875% | 0.3125% to 1.875% |
| F6 | L | Bose-Hubbard (5), Fermi-Hubbard (1), TFIM (2) | 2.083% | 0.417% to 2.083% |
| F7 | S | UCCSD (1), GCM chemistry (1), three vibrational molecules (1 each), ten electronic molecules (1 or 2 each) | 0.333% | 0.167% to 0.333% |
| F7 | M | UCCSD (1) | 7.5% | 7.5% |
| F8 | M | quantum volume (1) | 4.6875% | 4.6875% |
| F8 | L | quantum volume (2), Clifford (1) | 3.906% | 1.953% to 3.906% |

| Heaviest and lightest circuits | Weight | Why |
| --- | ---: | --- |
| `Q/large/vqe_uccsd_n28` | 7.5% | F7's only medium circuit, and F7 has no large band |
| `S/test_QFT_100_transpile` | 6.25% | F1's only large circuit |
| `Q/large/32` | 4.6875% | F8's only medium circuit |
| `S/test_circSU2_89_transpile` | 3.906% | alone in its F4 large recipe |
| `S/test_clifford_100_transpile` | 3.906% | alone in its F8 large recipe |
| `F/gf2^4_mult.qasm` to `F/gf2^8_mult.qasm` | 0.056% each | five circuits sharing one of nine F2 small recipes |

The exact fraction for each of the 136 circuits is in section 4 of the
specification.

## Compiled but carrying no design weight

The six marginal-routing candidates have zero weight in the specification until
a routing preflight admits them. The harness has no preflight step and admits
them like any other circuit once their baseline compiles, so a clean `full` run
reports 142 scored circuits and recomputes the weights over those.

| Circuit | Family | Why no design weight | In the harness |
| --- | :---: | --- | --- |
| `Q/small/qpe_n9` | F1 | under 10 SWAPs on some target in the historical probe | scored once baseline compiles |
| `Q/medium/bv_n14` | F3 | same | scored once baseline compiles |
| `Q/medium/cc_n12` | F3 | same | scored once baseline compiles |
| `Q/large/bv_n30` | F3 | same | scored once baseline compiles |
| `H/ham_7-uf20-0384.cnf-8-res` | F5 | same | scored once baseline compiles |
| `H/ham_mu_x_prime_enc_stdbinary_dvalues_8-8-8-8-8-8-8-8-8-4-4-4-4-4-4` | F7 | same | scored once baseline compiles |
| `Q/medium/knn_n25` | F3 | same two-qubit gates as `Q/medium/swap_test_n25` | weight 0, always |
| `Q/large/knn_n41` | F3 | same two-qubit gates as `Q/large/swap_test_n41` | weight 0, always |
| `H/ham_ham_JW10` | F7 | same Pauli strings as `H/ham_ham_JW-10` | weight 0, always |
| `H/ham_tsp_prob-st70_Ncity-10_enc-unary` | F5 | same Pauli strings as the `pr76` instance | weight 0, always |
| `H/ham_tsp_prob-kroD100_Ncity-16_enc-stdbinary` | F5 | same Pauli strings as the `fl417` instance | weight 0, always |

Eight more circuits from the studies are not compiled at all because they embed
without routing on at least one target; section 5 of the specification lists
them.

## Big circuits

Each compile of these six runs as its own LSF job. Times and memory are the
worst seed in the 2026-11-10 baseline screening. `bwt_n37` is why the default
worker memory is 10 GiB.

| Circuit | Compile | Peak memory |
| --- | ---: | ---: |
| `Q/large/bwt_n37` | about 1,500 s | about 9 GB |
| `Q/large/vqe_uccsd_n28` | about 355 s | about 2 GB |
| `F/hwb12.qasm` | about 346 s | about 2.6 GB |
| `Q/medium/bwt_n21` | about 339 s | about 2.5 GB |
| `Q/medium/factor247_n15` | about 338 s | about 2.3 GB |
| `F/hwb11.qasm` | about 178 s | |

## Rules that change the score

| Rule | Effect |
| --- | --- |
| Admission reads baseline results only | a circuit is dropped if any of its 6 cases fails in any baseline seed; weights are recomputed over the rest |
| A family ends up empty | no score, unless `--allow-missing-families` |
| Evolve fails where baseline succeeded | the circuit stays; the main score is unavailable and the report shows a labelled partial score |
| Validation is target legality only | no equivalence check; matters most for `cc_n32`/`cc_n64` (control flow), `bwt_n21`/`bwt_n37` (resets), `qec9xz_n17`/`seca_n11` (mid-circuit measurement) |
| Hamiltonian circuits (F5, F6, F7) | post-routing optimization compresses Pauli ladders, so N2 can under-report routing cost; read the SWAP counts too |
