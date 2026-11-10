# Validation coverage and remaining delivery gates

This file describes implemented capabilities, not a claim that the complete
native corpus has passed admission. Checks have four outcomes: `verified`,
`mismatch`, `unsupported`, or `error`. Only `verified` satisfies a required gate.
Compilation success, target legality, and routing replay alone do not prove
whole-circuit semantic equivalence.

## Frozen contract (`legality-routing-compositional-semantics-v3`)

Every accepted observation requires four `verified` certificates: recursive
native target legality (directed qubits, parameters, conditions, reset and
measurement support), injective in-range initial/final layouts, and exact
routing replay at every executed SABRE boundary (or an acknowledged bypass), plus
semantic equivalence. The semantic certificate may use the exact bounded oracles
below or compositional evidence covering every compiler transformation. A
`mismatch`, `error` or `unsupported` semantic result invalidates the observation.
Reports retain each certificate's scope and outcome. Legality and routing alone
cannot replace end-to-end semantic evidence.

Protocol v3 restores the implementation plan's required semantic gate. Earlier
v2 results that treated unsupported semantics as disclosed coverage are not
compatible with this contract and cannot be reused as validated observations.

Fixtures declare arbitrary logical input states and zero-initialized compiler
ancillas. Both full presets therefore explicitly receive
`qubits_initially_zero=False`; the setting is part of the transpilation contract
and case identity. Qiskit's default `True` allows high-level synthesis to borrow
otherwise unused logical inputs as clean ancillas, which is incompatible with
the declared arbitrary-input contract. This common setting preserves all preset
stages, the source builds' algorithms, and the frozen fixture semantics.

The supplied Qiskit 2.6 source presets include
`RemoveDiagonalGatesBeforeMeasure`, but no longer include
`RemoveResetInZeroState` or `OptimizeSwapBeforeMeasure` in their default L2/L3
initialization stages. The separately pinned Qiskit 2.5 integration environment
has different preset composition; its test results do not establish actual-build
coverage. A pass certificate must check the supplied build's executed stages.

Native SABRE start diagnostics also distinguish source-backed policy from the
Python-call arguments. The reviewed supplied builds share
`crates/transpiler/src/passes/sabre/layout.rs` SHA-256
`429cd7803bdf2c005ce2469834ed60de12d2726df4f537d70e5cff697182b140`.
On connected targets this implementation adds dense, ascending, and descending
templates to the 20 random starts and any supplied partial templates. Physical
widths 127, 133, and 156 add a source-defined ring template; FakeTorino therefore
has 24 layout trials when no extra partial templates are supplied. Dense and
ring templates may leave locations unspecified, which native code completes with
the trial's seeded shuffle. Diagnostics preserve those templates, completion
policy, counts, and source identity without claiming the native ABI exposes the
winning trial. Unreviewed source policies remain explicitly unsupported.

| Check | Implemented scope | Limit |
| --- | --- | --- |
| Typed fixture identity | Logical operations, registers, wire maps, conditions, branches, parameters and supported high-level operations | Unknown operations/representations fail compatibility checks |
| N2 | Final native two-qubit instruction count; both conditional bodies counted once | Unknown loops are unsupported |
| D2 | Weighted longest dependency path, quantum/classical dependencies, zero-cost barriers and mapped branch maximum | Structural depth, not expected dynamic latency |
| Target legality | Recursive operation support, directed physical qubits and fixed parameters | Legality is independent of equivalence |
| Layout | Injective in-range initial/final maps | Ancilla and observable semantics require additional checks |
| Routing replay | Dependency-preserving operations, original versus inserted SWAPs, measurement/reset events and reconciled branch exits | Does not certify synthesis or final optimization |
| Static semantics | Arbitrary-input basis-isometry comparison with zero ancillas and preserved terminal measurement events | At most eight logical and fourteen active physical qubits |
| Dynamic semantics | Arbitrary-input quantum instrument comparison, including measurement, reset and conditions | At most four logical and six active physical qubits; larger circuits require a scalable certificate |
| Compositional semantics | Independently checked local rewrites and routing identities across compiler boundaries | Every changed boundary must be certified; unsupported transformations fail the semantic gate |

The exact instrument validator includes dynamic semantic tests that reject
altered branch behavior. This is stronger than checking only an all-zero input,
but it is still a bounded validator. Full-corpus fixtures such as `cc_n32/64`,
`bwt_n21/37`, `qec9xz_n17`, and `seca_n11` require supported scalable evidence
covering synthesis and optimization as well as routing. They must not be admitted
merely because compilation succeeds; complete current-protocol certificates must
be retained for each accepted observation.

The catalogue's native inputs start at eight qubits. A static eight-qubit input
may fit the isometry oracle when its physical footprint also fits; most native
inputs exceed the bounds. The bounded validators do **not** establish complete
semantic validation for the requested native-corpus comparison. Larger inputs
need verified compositional evidence in addition to legality, layout and exact
routing replay. Normal preparation stops at unresolved
eligibility when a required certificate or compilation evidence is missing. An
explicit `--accept-unresolved` decision can record excluded candidates and a
conditional cohort; it cannot mark unsupported checks verified or repair an
empty-family freeze gate. An empty family needs the explicitly named reduced
design (`--allow-missing-families`), never silent reweighting.

## Remaining acceptance work

- Establish full-corpus coverage and adversarial evidence for compositional
  semantic certificates, with explicit stage coverage and dynamic assumptions.
- Validate both actual source versions' release/toolchain compatibility,
  typed import round trips, SABRE settings and instrumentation invariance.
- Complete all 2,682 baseline preflight observations (149 candidates × three
  targets × two levels × three seeds), plus separate resource pilots; retain
  every admitted, excluded, and unresolved disposition.
- Run real local A/A observations, interruption/resume and cross-run baseline
  reuse with independent measurements in each arm.
- Verify lease/publication behavior on the actual shared filesystem and run
  small real-LSF A/A, unknown-submission, and cancellation/recovery exercises.
- Complete the frozen-cohort rehearsal and publish actual coverage, resource
  use, concentrations and validation scope beside its scores.

Ordinary tests use synthetic scientific records, adversarial small fixtures and
fake scheduler responses. They test calculations and mechanisms; they cannot
substitute for native-corpus or site-specific acceptance measurements.
