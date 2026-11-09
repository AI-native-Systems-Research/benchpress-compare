# Validation coverage and remaining delivery gates

This file describes implemented capabilities, not a claim that the complete
native corpus has passed admission. Required checks have four outcomes:
`verified`, `mismatch`, `unsupported`, or `error`. Only `verified` satisfies a
required gate. Compilation success, target legality, and routing replay alone
do not prove whole-circuit semantic equivalence.

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

The exact instrument validator includes dynamic semantic tests that reject
altered branch behavior. This is stronger than checking only an all-zero input,
but it is still a bounded validator. Full-corpus fixtures such as `cc_n32/64`,
`bwt_n21/37`, `qec9xz_n17`, and `seca_n11` require supported scalable evidence
covering synthesis and optimization as well as routing. That evidence is not
implemented. They must not be admitted merely because compilation succeeds.

The catalogue's native inputs start at eight qubits. A static eight-qubit input
may fit the isometry oracle when its physical footprint also fits; most native
inputs exceed the bounds. The bounded validators do **not** establish complete
semantic validation for the requested native-corpus comparison. Current normal preparation must stop at unresolved
eligibility when it cannot obtain required evidence. An explicit
`--accept-unresolved` decision can record excluded candidates and a conditional
cohort; it cannot mark unsupported checks verified or repair an empty-family
freeze gate.

## Remaining acceptance work

- Implement and adversarially test scalable semantic certificates for the native
  transformations, with explicit stage coverage and dynamic assumptions.
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
