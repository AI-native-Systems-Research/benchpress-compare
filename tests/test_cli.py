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
    assert output["expected_preflight_observations"] == 2682
    result = subprocess.run([sys.executable, "-c", "import sys; import benchpress_compare.cli; assert 'qiskit' not in sys.modules"],
                            capture_output=True, text=True)
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
    config = RunConfig(baseline="/deleted/source-a", evolve="/deleted/source-b", store=str(tmp_path / "store"), results_root=str(root))
    atomic_write_json(root / "request.json", {"schema_version": 1, "config": config.to_dict()})
    saved = _config(parser().parse_args(["run", "--results-root", str(root)]))
    assert saved.baseline == "/deleted/source-a"
    assert main(["run", "--results-root", str(root), "--seeds", "7"]) == 2
    assert "immutable" in capsys.readouterr().err


def test_explicit_unresolved_decision_is_allowed_on_resume(tmp_path):
    from benchpress_compare.cli import _config
    from benchpress_compare.config import RunConfig
    root = tmp_path / "run"
    cfg = RunConfig(baseline="/sources/a", evolve="/sources/b", store=str(tmp_path / "store"), results_root=str(root))
    atomic_write_json(root / "request.json", {"schema_version": 1, "config": cfg.to_dict()})
    supplied = _config(parser().parse_args(["resume", "--results-root", str(root), "--accept-unresolved"]))
    assert supplied.accept_unresolved
    assert not json.loads((root / "request.json").read_text())["config"]["accept_unresolved"]


def test_report_reads_committed_results_without_original_harness(tmp_path, capsys):
    from benchpress_compare.identity import canonical_hash
    from benchpress_compare.store import publish_bundle
    root = tmp_path / "run"
    case = {"case_id": "c", "input_id": "sample", "family": "F1", "band": "S", "recipe": "QFT",
            "level": 2, "target_class": "square", "target_id": "square9", "seeds": [1],
            "weight": {"numerator": 1, "denominator": 8}}
    plan = {"schema_version": 1, "design": "benchpress-smoke-v1", "cases": [case],
            "observations": [{}, {}], "config": {}, "builds": {}, "weights": {}, "pilot": {},
            "coordinator_hash": "an-old-harness-which-is-unneeded-for-reporting"}
    plan["plan_hash"] = canonical_hash(plan)
    atomic_write_json(root / "plan.json", plan)
    (root / "plan.sha256").write_text(plan["plan_hash"] + "\n")
    for arm in ("baseline", "evolve"):
        publish_bundle(root / "observations" / arm, {"schema_version": 1, "case_id": "c",
            "observation_id": arm, "arm": arm, "seed": 1, "status": "SUCCEEDED", "N2": 10, "D2": 5})
    assert main(["report", "--results-root", str(root), "--family", "F1"]) == 0
    paths = json.loads(capsys.readouterr().out)
    report = json.loads(open(paths["summary.json"]).read())
    assert report["summary"]["metrics"]["N2"]["score"] == 1
    assert report["summary"]["metrics"]["D2"]["circuit_counts"]["same"] == 1
