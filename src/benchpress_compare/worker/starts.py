"""Source-attested provenance for native SABRE's implicit starting layouts.

The native ABI exposes user-supplied partial layouts, not its internal heuristic
starts. For reviewed source content we reconstruct the templates, using the
same build's dense-layout primitive. This observes policy without changing the
native call or consuming any RNG state.
"""

# Start-template reconstruction is adapted from Qiskit's SABRE layout policy:
# (C) Copyright IBM 2022, licensed under the Apache License, Version 2.0
# (https://www.apache.org/licenses/LICENSE-2.0). This adapter is modified to
# record provenance only; it does not implement or replace the native search.

from __future__ import annotations

from pathlib import Path
import re

from benchpress_compare.errors import IntegrityError
from benchpress_compare.identity import canonical_hash, file_hash, read_json

SOURCE_FILE = "crates/transpiler/src/passes/sabre/layout.rs"
REVIEWED_SOURCE_HASHES = {
    # Identical in the supplied baseline and evolve Qiskit 2.6 source snapshots.
    "429cd7803bdf2c005ce2469834ed60de12d2726df4f537d70e5cff697182b140",
}
START_PROTOCOL = "source-attested-sabre-start-templates-v1"


def reviewed_start_source(manifest_path=None):
    if manifest_path is None:
        return {"status": "not_requested", "reason": "no_attested_source_build"}
    build = read_json(manifest_path)
    snapshot = Path(build["snapshot_path"])
    source = read_json(snapshot / "snapshot.json")
    if (
        source.get("tree_hash") != build["snapshot_hash"]
        or canonical_hash(source["files"]) != build["snapshot_hash"]
    ):
        raise IntegrityError("SABRE source snapshot differs from the attested build")
    path = snapshot / "source" / SOURCE_FILE
    entries = [entry for entry in source["files"] if entry["path"] == SOURCE_FILE]
    if len(entries) != 1 or not path.is_file() or file_hash(path) != entries[0].get("sha256"):
        raise IntegrityError("SABRE layout source differs from the pinned snapshot")
    digest = entries[0]["sha256"]
    evidence = {
        "protocol": START_PROTOCOL,
        "source_file": SOURCE_FILE,
        "source_sha256": digest,
        "snapshot_hash": build["snapshot_hash"],
    }
    if digest not in REVIEWED_SOURCE_HASHES:
        return {**evidence, "status": "unsupported", "reason": "unreviewed_native_start_policy"}
    # Ring lists come from the pinned source rather than a second copied list.
    rings = {
        width: [int(value) for value in re.findall(r"\d+", contents)]
        for width, contents in re.findall(
            r"if num_physical_qubits == (\d+) \{.*?starting_layouts\.push\(\s*\[([\d,\s]+)\]",
            path.read_text(),
            flags=re.DOTALL,
        )
    }
    if set(rings) != {"127", "133", "156"}:
        raise IntegrityError("Reviewed native ring templates could not be reconstructed")
    return {**evidence, "status": "verified", "ring_templates": rings}


def native_start_templates(target, logical_width, arguments, source_record):
    """Describe starts only when their source and connected-target path are known.

    Dense and ring templates can leave physical ancillas unspecified. Native
    layout_trial fills them by a seeded shuffle; they are deliberately reported
    as templates, not as completely deterministic initial permutations.
    """
    evidence = {
        "protocol": START_PROTOCOL,
        "source": {key: value for key, value in source_record.items() if key != "ring_templates"},
        "random_trials": arguments["num_random_trials"],
        "supplied_partial_templates": [list(row) for row in arguments.get("partial_layouts", [])],
    }
    if source_record.get("status") != "verified":
        return {**evidence, "status": "unsupported", "reason": "native_start_source_unverified"}
    coupling = target.build_coupling_map()
    if coupling is None or coupling.size() != target.num_qubits or not coupling.is_connected():
        return {**evidence, "status": "unsupported", "reason": "native_target_split_not_attested"}
    import numpy as np
    from qiskit._accelerate.dense_layout import best_subset

    physical_width = target.num_qubits
    adjacency = np.equal(coupling.distance_matrix, 1).astype(float)
    dense = best_subset(logical_width, adjacency, 0, 0, False, True, np.zeros((1, 1)))[2]
    templates = [
        {"name": "dense", "layout": [int(q) for q in dense]},
        {"name": "ascending", "layout": list(range(physical_width))},
        {"name": "descending", "layout": list(reversed(range(physical_width)))},
    ]
    if (ring := source_record["ring_templates"].get(str(physical_width))) is not None:
        templates.append({"name": f"ring_{physical_width}", "layout": list(ring)})
    return {
        **evidence,
        "status": "verified",
        "evidence_kind": "reviewed_source_policy_and_reconstructed_templates",
        "logical_width": logical_width,
        "physical_width": physical_width,
        "implicit_templates": templates,
        "implicit_template_count": len(templates),
        "total_layout_trials": arguments["num_random_trials"]
        + len(evidence["supplied_partial_templates"]) + len(templates),
        "template_order": "random, supplied partial, dense, ascending, descending, optional ring",
        "completion": "Unspecified physical locations use the native trial seed's PCG64Mcg shuffle.",
        "internal_winning_trial": "not exposed by the native ABI",
    }
