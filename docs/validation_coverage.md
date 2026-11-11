# Validation coverage and remaining delivery gates

This file describes implemented capabilities, not a claim that the complete
native corpus has passed admission. Checks have four outcomes: `verified`,
`mismatch`, `unsupported`, or `error`. Only `verified` satisfies a required gate.
Target legality does not prove that compilation preserves the input circuit's
behavior.

## Frozen contract (`target-legality-v4`)

Every accepted observation requires a `verified` target-legality check. It checks
native operation identity, directed physical qubits, fixed parameters, capacity,
and support for operations inside conditional branches, including reset and
measurement. A `mismatch`, `error`, or `unsupported` legality result invalidates
the observation. Reports retain the check's scope and outcome.

Layout validation, routing replay, and semantic equivalence checks are removed.
There are no bounded simulation or pass-by-pass equivalence certificates in the
current validation contract. Layout maps may be recorded as diagnostic output;
they do not provide a validation certificate. Earlier protocol results cannot be
reused as current-protocol observations because validation and instrumentation
identities have changed.

Instrumentation `sabre-native-counts-v3` still records SABRE execution and counts
SWAPs before and after each native routing boundary. The count delta supplies the
routing burden used for admission, with recursive counts for conditional bodies.
This is an activity/count diagnostic, not a proof that routing preserves behavior.
Missing or unsupported required routing diagnostics can still leave admission
unresolved, independently of target legality.

Fixtures declare arbitrary logical input states and zero-initialized compiler
ancillas. Both full presets therefore explicitly receive
`qubits_initially_zero=False`; the setting is part of the transpilation contract
and case identity. Qiskit's default `True` allows high-level synthesis to borrow
otherwise unused logical inputs as clean ancillas, which is incompatible with
the declared arbitrary-input contract. This common setting preserves all preset
stages, the source builds' algorithms, and the frozen fixture semantics.

The separately pinned Qiskit 2.5 integration environment has different preset
composition from the supplied Qiskit 2.6 builds. Its test results do not establish
actual-build compatibility or instrumentation coverage.

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
| Routing diagnostics | SABRE execution, native-boundary SWAP count deltas, trial/start settings and bypass evidence | Used for admission and attribution; no routing replay or correctness proof |
| Layout diagnostics | Recorded initial/final maps where available | Not validated and not a required check |

Dynamic fixtures such as `cc_n32/64`, `bwt_n21/37`, `qec9xz_n17`, and `seca_n11`
require supported imports, metrics, native target operations, and routing
diagnostics. They do not require a proof of semantic equivalence. A circuit can
pass target legality while computing a different result; the benchmark does not
detect that class of compiler error.

Normal preparation stops at unresolved eligibility when required legality,
metric, routing-diagnostic, or compilation evidence is missing. An
explicit `--accept-unresolved` decision can record excluded candidates and a
conditional cohort; it cannot mark unsupported checks verified or repair an
empty-family freeze gate. An empty family needs the explicitly named reduced
design (`--allow-missing-families`), never silent reweighting.

## Remaining acceptance work

- Validate both actual source versions' release/toolchain compatibility,
  typed import round trips, SABRE settings and instrumentation invariance.
- Complete all 2,646 baseline preflight observations (147 candidates × three
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
