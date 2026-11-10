"""Compose independently checked semantic identities across every compiler pass."""

from __future__ import annotations

from benchpress_compare.corpus.ir import export_circuit
from benchpress_compare.identity import canonical_hash
from .compositional import local_equivalence


class PassCertificates:
    """Record a fail-closed chain from the logical input to the final output.

    Each link maps every old wire's input and output location to the next
    boundary. Composing those maps also checks Qiskit's reported final layout;
    a collection of valid local proofs alone cannot conceal an incorrect layout.
    """

    def __init__(self, original_circuit, diagnostics, artifacts):
        self.previous = export_circuit(original_circuit)
        self.previous_qubits = list(original_circuit.qubits)
        self.original_hash = canonical_hash(self.previous)
        self.previous_hash = self.original_hash
        self.initial = list(range(original_circuit.num_qubits))
        self.final = list(range(original_circuit.num_qubits))
        self.properties = {}
        self.diagnostics = diagnostics
        self.artifacts = artifacts
        self.boundaries = []
        self.native_count = 0

    @staticmethod
    def _properties(property_set):
        return {
            key: dict(value.get_virtual_bits())
            for key in ("layout", "final_layout", "virtual_permutation_layout", "post_layout")
            if (value := property_set.get(key)) is not None
        }

    def observe(self, pass_, dag, property_set):
        from qiskit.converters import dag_to_circuit

        name = type(pass_).__name__
        properties = self._properties(property_set)
        # Analysis passes cannot modify the DAG under the pass-manager contract.
        # Preserve their chosen maps before the following pass consumes them.
        if pass_.is_analysis_pass:
            self.properties = properties
            return
        current = export_circuit(dag_to_circuit(dag))
        n = self.previous["num_qubits"]
        initial = list(range(n))
        final = list(initial)
        before_hash, after_hash = self.previous_hash, canonical_hash(current)
        proof = None
        try:
            if name == "ApplyLayout":
                mapping = self.properties.get("post_layout", self.properties.get("layout"))
                if mapping is None or any(q not in mapping for q in self.previous_qubits):
                    raise ValueError("ApplyLayout lacks a complete preceding layout map")
                initial = [mapping[q] for q in self.previous_qubits]
                final = list(initial)
            elif name == "ElidePermutations":
                old = self.properties.get("virtual_permutation_layout", {})
                new = properties.get("virtual_permutation_layout", {})
                old_values = [old.get(q, i) for i, q in enumerate(self.previous_qubits)]
                new_values = [new.get(q, i) for i, q in enumerate(dag.qubits)]
                if set(old_values) != set(new_values) or len(set(new_values)) != n:
                    raise ValueError("Permutation elision lacks an injective wire correspondence")
                inverse = {value: i for i, value in enumerate(new_values)}
                final = [inverse[value] for value in old_values]
            elif name in {"SabreLayout", "SabreSwap"}:
                calls = self.diagnostics["native_calls"][self.native_count:]
                if len(calls) == 1 and calls[0].get("replay", {}).get("status") == "verified":
                    artifact = self.artifacts.get(f"routing-{self.native_count}.json")
                    if artifact is None:
                        raise ValueError("SABRE has no retained routing artifact")
                    initial, final = artifact["initial"], artifact["final"]
                    if (
                        canonical_hash(artifact["before"]) == before_hash
                        and canonical_hash(artifact["after"]) == after_hash
                    ):
                        proof = {
                            "status": "verified",
                            "method": "exact_routing_replay",
                            "artifact": f"routing-{self.native_count}.json",
                        }
                    else:
                        raise ValueError("SABRE replay boundaries differ from the observed pass")
                elif before_hash != after_hash:
                    raise ValueError("SABRE transformation lacks one complete native replay")
            if proof is None:
                if before_hash == after_hash and initial == final == list(range(n)):
                    proof = {"status": "verified", "method": "identical_boundary"}
                else:
                    proof = local_equivalence(self.previous, current, initial, final)
            self.initial = [initial[q] for q in self.initial]
            self.final = [final[q] for q in self.final]
        except (ValueError, KeyError, IndexError) as exc:
            proof = {"status": "unsupported", "reason": "unverified_pass_mapping", "detail": str(exc)}
        self.boundaries.append({
            "pass": name, "before_hash": before_hash, "after_hash": after_hash,
            "initial": initial, "final": final, "proof": proof,
        })
        self.previous = current
        self.previous_hash = after_hash
        self.previous_qubits = list(dag.qubits)
        self.properties = properties
        self.native_count = len(self.diagnostics["native_calls"])

    def finish(self, output):
        final_ir = export_circuit(output)
        final_hash = canonical_hash(final_ir)
        if final_hash != self.previous_hash:
            proof = local_equivalence(self.previous, final_ir)
            self.boundaries.append({
                "pass": "final_output", "before_hash": self.previous_hash,
                "after_hash": final_hash, "proof": proof,
            })
        expected_initial = (
            list(output.layout.initial_index_layout(filter_ancillas=True))
            if output.layout is not None else list(range(len(self.initial)))
        )
        expected_final = (
            list(output.layout.final_index_layout(filter_ancillas=True))
            if output.layout is not None else list(range(len(self.final)))
        )
        layout = {
            "status": "verified" if (self.initial, self.final) == (expected_initial, expected_final)
            else "mismatch",
            "composed_initial": self.initial, "composed_final": self.final,
            "reported_initial": expected_initial, "reported_final": expected_final,
        }
        failures = [row for row in self.boundaries if row["proof"]["status"] != "verified"]
        error_bound = sum(row["proof"].get("error_bound", 0.0) for row in self.boundaries)
        status = failures[0]["proof"]["status"] if failures else layout["status"]
        if status == "verified" and error_bound > 1e-8:
            status = "unsupported"
        result = {
            "status": status, "scope": {"method": "per_pass_compositional_chain",
            "input_state": "arbitrary", "ancillas": "arbitrary idle wires",
            "classical_outputs": "all", "covered_stages": "every transformation pass"},
            "input_hash": self.original_hash, "output_hash": final_hash,
            "error_bound": error_bound, "tolerance": 1e-8,
            "boundaries": len(self.boundaries), "layout": layout,
            "unverified": [{"pass": row["pass"], "proof": row["proof"]} for row in failures],
            "artifact": "semantic-passes.json",
        }
        if error_bound > 1e-8:
            result["reason"] = "composed_numerical_error_bound_exceeded"
        self.artifacts["semantic-passes.json"] = {
            "schema_version": 1, "boundaries": self.boundaries, "summary": result,
        }
        return result
