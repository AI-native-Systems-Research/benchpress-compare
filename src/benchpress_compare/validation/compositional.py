"""Compositional equivalence certificates from bounded causal unitary regions.

The checker never simulates a whole wide circuit. Equal dependency-frontier
events cancel, and changed unitary regions are checked on all basis inputs of
their support. Measurements, resets, and classical conditions remain explicit
ordered events. An exhausted support bound is unsupported, not equivalence.
"""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from functools import lru_cache
import json

from benchpress_compare.identity import canonical_hash

CERTIFICATE_PROTOCOL = "causal-local-unitary-v1"


def _wires(op):
    return frozenset(
        [("q", q) for q in op.get("qargs", [])]
        + [("c", c) for c in op.get("cargs", [])]
        + [("c", c) for c in op.get("condition", {}).get("bits", [])]
    )


def _unitary(op):
    return not (
        op.get("cargs") or op.get("condition") or op.get("blocks")
        or op["name"] in {"measure", "reset", "initialize"}
    )


def _operations(circuit, qmap, cmap):
    """Inline frozen definitions, retaining classical events and branch scopes."""
    result = []
    for source in circuit["operations"]:
        if source.get("condition") and source.get("kind") != "if_else":
            raise ValueError("Only explicit if_else operations may carry a classical condition")
        op = deepcopy(source)
        op["qargs"] = [qmap[q] for q in source.get("qargs", [])]
        op["cargs"] = [cmap[c] for c in source.get("cargs", [])]
        if source.get("condition"):
            op["condition"]["bits"] = [cmap[c] for c in source["condition"]["bits"]]
        if op.get("kind") in {"barrier", "delay"}:
            continue
        if op.get("kind") == "definition":
            result.extend(_operations(op["definition"], op["qargs"], op["cargs"]))
        elif op.get("kind") == "pauli_evolution":
            from .pauli import expand_pauli_evolution

            result.extend(expand_pauli_evolution(op))
        else:
            result.append(op)
    return result


class _Frontier:
    def __init__(self, operations):
        self.ops = operations
        self.queues = defaultdict(deque)
        for i, op in enumerate(operations):
            for wire in _wires(op):
                self.queues[wire].append(i)
        self.remaining = set(range(len(operations)))
        self.front = {i for i, op in enumerate(operations)
                      if all(self.queues[w][0] == i for w in _wires(op))}

    def ready(self):
        return sorted(self.front)

    def remove(self, indices):
        for i in sorted(indices):
            self.remaining.remove(i)
            self.front.remove(i)
            for wire in _wires(self.ops[i]):
                assert self.queues[wire].popleft() == i
            for wire in _wires(self.ops[i]):
                if self.queues[wire]:
                    j = self.queues[wire][0]
                    if all(self.queues[w][0] == j for w in _wires(self.ops[j])):
                        self.front.add(j)

    def prefix(self, support):
        """Maximal causal prefix of unitary operations contained in support."""
        import heapq

        chosen, blocked = [], set()
        iterators = {("q", q): iter(self.queues[("q", q)]) for q in support}
        heads = {wire: next(iterator, None) for wire, iterator in iterators.items()}
        ready = [i for i in self.front if set(self.ops[i]["qargs"]) <= support]
        heapq.heapify(ready)
        queued = set(ready)
        while ready:
            i = heapq.heappop(ready)
            op = self.ops[i]
            if not _unitary(op) or not set(op["qargs"]) <= support:
                blocked.add(i)
                continue
            chosen.append(i)
            for wire in _wires(op):
                heads[wire] = next(iterators[wire], None)
            for wire in _wires(op):
                j = heads[wire]
                if j is None or j in queued:
                    continue
                candidate_wires = _wires(self.ops[j])
                if not candidate_wires <= set(heads):
                    blocked.add(j)
                elif all(heads[w] == j for w in candidate_wires):
                    heapq.heappush(ready, j)
                    queued.add(j)
        blocked.update(i for wire, i in heads.items() if i is not None)
        return chosen, blocked


@lru_cache(maxsize=4096)
def _matrix(encoded, width):
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import Operator
    from benchpress_compare.corpus.ir import import_circuit, IR_PROTOCOL

    operations = json.loads(encoded)
    circuit = {
        "schema_version": 1, "protocol": IR_PROTOCOL, "num_qubits": width,
        "num_clbits": 0, "global_phase": 0, "qregs": [], "cregs": [],
        "operations": operations,
    }
    return Operator(import_circuit(circuit) if operations else QuantumCircuit(width)).data


def _region_matrix(frontier, indices, support):
    mapping = {q: i for i, q in enumerate(sorted(support))}
    ops = []
    for i in indices:
        op = deepcopy(frontier.ops[i])
        op["qargs"] = [mapping[q] for q in op["qargs"]]
        ops.append(op)
    return _matrix(json.dumps(ops, sort_keys=True, separators=(",", ":")), len(support))


def _equal_up_to_phase(a, b, tolerance):
    import numpy as np

    index = np.unravel_index(np.argmax(np.abs(a)), a.shape)
    if abs(b[index]) <= tolerance:
        return False, float("inf")
    phase = b[index] / a[index]
    phase /= abs(phase)
    error = float(np.max(np.abs(b - phase * a)))
    return error <= tolerance, error


def _matching_prefix(a, ai, b, bi, support):
    """Find a certified cut before basis expansions spill into the next region."""
    import numpy as np

    am, bm = _region_matrix(a, ai, support), _region_matrix(b, bi, support)
    same, error = _equal_up_to_phase(am, bm, 1e-8)
    if same:
        return ai, bi, error

    def prefixes(frontier, indices):
        current = np.eye(2 ** len(support), dtype=complex)
        yield 0, current
        # A bounded number of retained prefix matrices caps certificate memory.
        wanted = set(range(min(32, len(indices)) + 1)) | set(
            range(max(1, len(indices) - 32), len(indices) + 1)
        )
        for length, index in enumerate(indices, 1):
            current = _region_matrix(frontier, [index], support) @ current
            if length in wanted:
                yield length, current

    # Phase-invariant numerical buckets are only a search optimization. Every
    # candidate still undergoes the full residual check before acceptance.
    def fingerprint(matrix):
        flat = matrix.ravel()
        index = next(i for i, value in enumerate(flat) if abs(value) > 1e-6)
        normalized = matrix * (abs(flat[index]) / flat[index])
        real, imag = np.round(normalized.real, 6), np.round(normalized.imag, 6)
        real[real == 0] = 0.0
        imag[imag == 0] = 0.0
        return real.tobytes(), imag.tobytes()

    indexed = defaultdict(list)
    for length, matrix in prefixes(a, ai):
        indexed[fingerprint(matrix)].append((length, matrix))
    best = None
    for nb, matrix_b in prefixes(b, bi):
        for na, matrix_a in indexed.get(fingerprint(matrix_b), []):
            if na + nb == 0 or best is not None and na + nb <= best[0] + best[1]:
                continue
            same, error = _equal_up_to_phase(matrix_a, matrix_b, 1e-8)
            if same:
                best = na, nb, error
    if best is not None:
        return ai[:best[0]], bi[:best[1]], best[2]

    def causal_cuts(frontier, indices, matrix):
        # A source-list prefix is too restrictive: a trailing basis gate on
        # wire B may occur after the first gate of the next rewrite on wire A.
        # Removing only DAG sinks enumerates valid causal cuts without requiring
        # disjoint gates to retain an arbitrary source-list order.
        pending = deque([(tuple(indices), matrix, 0)])
        seen = {tuple(indices)}
        while pending and len(seen) <= 512:
            selected, current, removed = pending.popleft()
            yield selected, current
            if removed >= 12:
                continue
            lasts = {}
            for i in selected:
                for wire in _wires(frontier.ops[i]):
                    lasts[wire] = i
            sinks = [i for i in set(lasts.values())
                     if all(lasts[w] == i for w in _wires(frontier.ops[i]))]
            for i in sinks:
                child = tuple(j for j in selected if j != i)
                if child in seen:
                    continue
                seen.add(child)
                gate = _region_matrix(frontier, [i], support)
                pending.append((child, gate.conj().T @ current, removed + 1))

    indexed = defaultdict(list)
    for selected, matrix in causal_cuts(a, ai, am):
        indexed[fingerprint(matrix)].append((selected, matrix))
    for selected_b, matrix_b in causal_cuts(b, bi, bm):
        for selected_a, matrix_a in indexed.get(fingerprint(matrix_b), []):
            if not selected_a and not selected_b:
                continue
            same, _ = _equal_up_to_phase(matrix_a, matrix_b, 1e-8)
            if same:
                # Recompute the selected regions directly for the numerical
                # certificate; inverse products above are search hints only.
                same, error = _equal_up_to_phase(
                    _region_matrix(a, selected_a, support),
                    _region_matrix(b, selected_b, support), 1e-8,
                )
                if same:
                    return selected_a, selected_b, error
    return None


def _local_equivalence(logical, output, initial=None, final=None, *, max_support=4):
    """Prove equality through local rewrites and exact classical event matching.

    ``initial`` and ``final`` map logical wire indices to output wire indices.
    SWAPs are represented as wire permutations on both sides. Consequently this
    also certifies permutation elision and layout application, with the final
    permutation checked rather than guessed. All inputs, including ancillas,
    are arbitrary; zero-initialized ancilla reasoning is deliberately excluded.
    """
    from benchpress_compare.metrics import validate_circuit
    from . import layout_validation

    validate_circuit(logical)
    validate_circuit(output)
    n, m = logical["num_qubits"], output["num_qubits"]
    initial = list(range(n)) if initial is None else list(initial)
    final = initial if final is None else list(final)
    layout = layout_validation(initial, final, n, m)
    scope = {"method": CERTIFICATE_PROTOCOL, "max_support": max_support,
             "input_state": "arbitrary", "tolerance": 1e-8}
    if layout["status"] != "verified":
        return dict(layout, scope=scope)
    if logical["num_clbits"] != output["num_clbits"]:
        return {"status": "mismatch", "reason": "classical_width_changed", "scope": scope}
    inverse = {p: q for q, p in enumerate(initial)}
    inverse.update({p: q for q, p in enumerate((p for p in range(m) if p not in initial), n)})
    try:
        left = _operations(logical, list(range(n)), list(range(logical["num_clbits"])))
        right = _operations(output, [inverse[p] for p in range(m)], list(range(output["num_clbits"])))
    except (KeyError, IndexError, TypeError, ValueError, NotImplementedError) as error:
        return {"status": "unsupported", "reason": "local_certificate_operation_unsupported",
                "detail": f"{type(error).__name__}: {error}", "scope": scope}

    def normalize_swaps(ops):
        permutation = list(range(m))
        normalized = []
        for source in ops:
            op = deepcopy(source)
            qs = op["qargs"]
            if op.get("kind") == "standard" and op["name"] == "swap" and _unitary(op) and len(qs) == 2:
                a, b = qs
                permutation[a], permutation[b] = permutation[b], permutation[a]
            else:
                op["qargs"] = [permutation[q] for q in qs]
                normalized.append(op)
        return normalized, permutation

    # Physical SWAP decompositions must remain local: pushing every SWAP to
    # the circuit end would spread a two-wire basis rewrite over the whole
    # routing workload. Only permutation-eliding passes need that normalization.
    if initial != final:
        left, lp = normalize_swaps(left)
        right, rp = normalize_swaps(right)
    else:
        lp = rp = list(range(m))
    # Materialize the relative exit permutation. Keeping this as actual SWAP
    # gates lets the local oracle also verify a SWAP synthesized to CX gates.
    desired = {lp[q]: rp[inverse[p]] for q, p in enumerate(final)}
    rest_keys = [q for q in range(m) if q not in desired]
    rest_values = [q for q in range(m) if q not in desired.values()]
    desired.update(zip(rest_keys, rest_values, strict=True))
    current = list(range(m))
    for i in range(m):
        if current[i] != desired[i]:
            j = current.index(desired[i])
            right.append({"name": "swap", "kind": "standard", "qargs": [i, j],
                          "cargs": [], "params": []})
            current[i], current[j] = current[j], current[i]
    a, b = _Frontier(left), _Frontier(right)
    regions, events, max_error, error_bound = 0, 0, 0.0, 0.0
    try:
        while a.remaining or b.remaining:
            ar, br = a.ready(), b.ready()
            found = False
            for i in ar:
                for j in br:
                    if a.ops[i] == b.ops[j]:
                        a.remove([i])
                        b.remove([j])
                        events += 1
                        found = True
                        break
                if found:
                    break
            if found:
                continue
            # Conditional bodies are certified independently for every branch;
            # their classical condition and quantum/classical bindings must agree.
            for i in ar:
                for j in br:
                    x, y = a.ops[i], b.ops[j]
                    if x.get("blocks") and y.get("blocks"):
                        header_x = {k: v for k, v in x.items() if k != "blocks"}
                        header_y = {k: v for k, v in y.items() if k != "blocks"}
                        if header_x != header_y or len(x["blocks"]) != len(y["blocks"]):
                            continue
                        children = [local_equivalence(u, v, max_support=max_support)
                                    for u, v in zip(x["blocks"], y["blocks"], strict=True)]
                        if all(c["status"] == "verified" for c in children):
                            a.remove([i])
                            b.remove([j])
                            events += 1
                            regions += sum(c.get("regions", 0) for c in children)
                            max_error = max([max_error, *(c.get("max_error", 0) for c in children)])
                            error_bound += sum(c.get("error_bound", 0) for c in children)
                            found = True
                            break
                if found:
                    break
            if found:
                continue
            seeds = [set(frontier.ops[i]["qargs"])
                     for frontier, ready in ((a, ar), (b, br)) for i in ready
                     if _unitary(frontier.ops[i])]
            if not seeds:
                return {"status": "unsupported", "reason": "classical_or_dynamic_event_changed",
                        "scope": scope}
            import heapq

            pending = [(len(s), tuple(sorted(s))) for s in seeds if len(s) <= max_support]
            pending = list(set(pending))
            heapq.heapify(pending)
            examined = set()
            while pending and len(examined) < 4096:
                _, support_tuple = heapq.heappop(pending)
                if support_tuple in examined:
                    continue
                examined.add(support_tuple)
                support = set(support_tuple)
                (ai, ab), (bi, bb) = a.prefix(support), b.prefix(support)
                if ai or bi:
                    matching = _matching_prefix(a, ai, b, bi, support)
                    if matching is not None:
                        matched_a, matched_b, error = matching
                        a.remove(matched_a)
                        b.remove(matched_b)
                        regions += 1
                        max_error = max(max_error, error)
                        # For dimension d, ||E||_2 <= ||E||_F <= d max|E_ij|.
                        # Unitary composition accumulates these bounds additively.
                        error_bound += 2 ** len(support) * error
                        found = True
                        break
                extensions = []
                for frontier, boundary in ((a, ab), (b, bb)):
                    for i in sorted(boundary):
                        op = frontier.ops[i]
                        qs = set(op["qargs"])
                        if _unitary(op) and qs & support and qs - support:
                            extensions.append(support | qs)
                for extension in extensions:
                    if len(extension) <= max_support:
                        key = tuple(sorted(extension))
                        if key not in examined:
                            heapq.heappush(pending, (len(extension), key))
            if not found:
                return {"status": "unsupported", "reason": "local_certificate_support_exhausted",
                        "scope": scope, "regions": regions, "events": events,
                        "remaining": [len(a.remaining), len(b.remaining)],
                        "frontier": [[a.ops[i]["name"] for i in ar[:4]],
                                     [b.ops[i]["name"] for i in br[:4]]]}
    except Exception as error:
        return {"status": "unsupported", "reason": "local_certificate_operation_unsupported",
                "detail": f"{type(error).__name__}: {error}", "scope": scope}
    if error_bound > 1e-8:
        return {"status": "unsupported", "reason": "certificate_numerical_budget_exhausted",
                "scope": scope, "error_bound": error_bound, "regions": regions}
    return {"status": "verified", "scope": scope, "regions": regions, "events": events,
            "max_error": max_error, "error_bound": error_bound,
            "covers": ["all_causal_regions", "classical_events",
                                                   "arbitrary_input_instrument"],
            "input_hash": canonical_hash(logical), "output_hash": canonical_hash(output)}


def local_equivalence(logical, output, initial=None, final=None, *, max_support=4):
    """Certify local rewrites, measured phases, and bounded gate commutations."""
    proof = _local_equivalence(logical, output, initial, final, max_support=max_support)
    if proof["status"] == "verified":
        return proof
    from .terminal import normalize_measured_diagonals
    from .commutation import normalize_commuting_rewrites

    a, left = normalize_measured_diagonals(logical, max_support=max_support)
    b, right = normalize_measured_diagonals(output, max_support=max_support)
    normalized = (
        _local_equivalence(a, b, initial, final, max_support=max_support)
        if left["removed"] or right["removed"] else proof
    )
    evidence = {"measurement_normalization": {"input": left, "output": right}}
    normalization_bound = left["error_bound"] + right["error_bound"]
    if normalized["status"] != "verified":
        a, commuting_left = normalize_commuting_rewrites(a, max_support=max_support)
        b, commuting_right = normalize_commuting_rewrites(b, max_support=max_support)
        normalized = _local_equivalence(a, b, initial, final, max_support=max_support)
        evidence["commutation_normalization"] = {"input": commuting_left, "output": commuting_right}
        normalization_bound += commuting_left["error_bound"] + commuting_right["error_bound"]
    if normalized["status"] != "verified":
        return {**proof, "normalization_attempt": {**evidence, "proof": normalized}}
    error_bound = normalized["error_bound"] + normalization_bound
    if error_bound > 1e-8:
        return {"status": "unsupported", "reason": "certificate_numerical_budget_exhausted",
                "error_bound": error_bound}
    return {**normalized, "error_bound": error_bound,
            "input_hash": canonical_hash(logical), "output_hash": canonical_hash(output),
            **evidence}
