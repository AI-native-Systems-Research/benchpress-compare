import json
import subprocess
import sys

from benchpress_compare.cli import main, parser
from benchpress_compare.identity import atomic_write_json


def test_help_and_four_path_requirement(tmp_path, capsys):
    assert main(["run", "--results-root", str(tmp_path / "run")]) == 2
    assert "requires --baseline" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()
    help_text = parser().format_help()
    assert "==SUPPRESS==" not in help_text
    assert "report" in help_text


def test_audit_is_coordinator_only(capsys):
    assert main(["audit"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["candidate_count"] == 149
    assert output["expected_observations"] == 5364
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import benchpress_compare.cli; assert 'qiskit' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_status_is_read_only(tmp_path, capsys):
    root = tmp_path / "absent"
    assert main(["status", "--results-root", str(root)]) == 0
    assert not root.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "NEW"


def test_resume_preserves_sources_and_rejects_seed_change(tmp_path, capsys):
    from benchpress_compare.cli import _config
    from benchpress_compare.config import RunConfig

    root = tmp_path / "run"
    config = RunConfig(
        baseline="/deleted/source-a",
        evolve="/deleted/source-b",
        store=str(tmp_path / "store"),
        results_root=str(root),
    )
    atomic_write_json(root / "request.json", {"schema_version": 1, "config": config.to_dict()})
    saved = _config(parser().parse_args(["run", "--results-root", str(root)]))
    assert saved.baseline == "/deleted/source-a"
    assert main(["run", "--results-root", str(root), "--seeds", "7"]) == 2
    assert "immutable" in capsys.readouterr().err


def test_report_reads_committed_results_without_original_harness(tmp_path, capsys):
    from itertools import product

    from benchpress_compare.identity import canonical_hash
    from benchpress_compare.store import publish_bundle
    from benchpress_compare.weights import LEVELS, TARGETS

    root = tmp_path / "run"
    cases = [
        {
            "case_id": f"c-{target}-{level}",
            "input_id": "sample",
            "family": "F1",
            "band": "S",
            "recipe": "QFT",
            "level": level,
            "target_class": target,
            "target_id": target,
            "seeds": [1],
        }
        for target, level in product(TARGETS, LEVELS)
    ]
    plan = {
        "schema_version": 1,
        "design": "benchpress-smoke-v1",
        "cases": cases,
        "observations": [{}, {}],
        "config": {"smoke": ["sample"]},
        "builds": {},
        "bundles": [],
        "coordinator_hash": "an-old-harness-which-is-unneeded-for-reporting",
    }
    plan["plan_hash"] = canonical_hash(plan)
    atomic_write_json(root / "plan.json", plan)
    (root / "plan.sha256").write_text(plan["plan_hash"] + "\n")
    for case in cases:
        for arm in ("baseline", "evolve"):
            publish_bundle(
                root / "observations" / f"{arm}-{case['case_id']}",
                {
                    "schema_version": 1,
                    "case_id": case["case_id"],
                    "observation_id": f"{arm}-{case['case_id']}",
                    "arm": arm,
                    "seed": 1,
                    "status": "SUCCEEDED",
                    "N2": 10,
                    "D2": 5,
                    "validation": {
                        "status": "verified",
                        "checks": {"target_legality": {"status": "verified"}},
                    },
                },
            )
    assert main(["report", "--results-root", str(root), "--family", "F1"]) == 0
    paths = json.loads(capsys.readouterr().out)
    report = json.loads(open(paths["summary.json"]).read())
    assert report["admission"]["admitted"] == ["sample"]
    assert report["summary"]["metrics"]["N2"]["score"] == 1
    assert report["summary"]["metrics"]["D2"]["circuit_counts"]["same"] == 1
