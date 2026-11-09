import csv
import json
import subprocess
import sys

from benchpress_compare.report import build_report, write_report


def fixture():
    cases = [
        {
            "case_id": "c",
            "input_id": "Q/circuit",
            "family": "F1",
            "band": "S",
            "recipe": "QFT",
            "level": 2,
            "target_class": "square",
            "target_id": "square9",
            "weight": {"numerator": 1, "denominator": 8},
            "sensitivity_weight": {"numerator": 1, "denominator": 8},
            "seeds": [1],
        }
    ]
    observations = [
        {
            "observation_id": arm,
            "case_id": "c",
            "arm": arm,
            "seed": 1,
            "status": "SUCCEEDED",
            "N2": n2,
            "D2": d2,
            "validation": {
                "status": "verified",
                "checks": [{"scope": "exact-unitary", "status": "verified"}],
            },
        }
        for arm, n2, d2 in (("baseline", 10, 10), ("evolve", 9, 11))
    ]
    return {
        "schema_version": 1,
        "cases": cases,
        "design": "benchpress-smoke-v1",
        "plan_hash": "sha",
    }, observations


def test_report_deterministic_authoritative_and_csv_roundtrip(tmp_path):
    plan, observations = fixture()
    report = build_report(plan, observations, status="COMPLETE")
    paths = write_report(tmp_path, report)
    before = {name: open(path, "rb").read() for name, path in paths.items()}
    write_report(tmp_path, build_report(plan, reversed(observations), status="COMPLETE"))
    assert before == {name: open(path, "rb").read() for name, path in paths.items()}
    saved = json.loads(before["summary.json"])
    assert saved["crossed_metric_circuits"] == ["Q/circuit"]
    assert saved["validation_coverage"]["statuses"] == {"verified": 2}
    assert "smoke subset" in before["report.md"].decode()
    with open(paths["cases.csv"], newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert json.loads(rows[0]["ratio"]) == {"numerator": 9, "denominator": 10}


def test_grouped_query_saved_and_unavailable_null():
    plan, observations = fixture()
    report = build_report(
        plan, observations[:1], query={"level": "L2"}, group_by=["family", "target"]
    )
    assert report["query"] == {"level": [2]}
    assert "family/target_class" in report["groups"]
    assert report["summary"]["metrics"]["N2"]["score"] is None
    assert "NaN" not in json.dumps(report, allow_nan=False)


def test_offline_import_does_not_import_qiskit():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import benchpress_compare.report; assert not any(k == 'qiskit' or k.startswith('qiskit.') for k in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_live_status_record_has_readable_status_and_preserves_execution_metadata():
    plan, observations = fixture()
    report = build_report(
        plan, observations, status={"status": "NEEDS_RECONCILIATION", "affected": ["job-1"]}
    )
    assert report["status"] == "NEEDS_RECONCILIATION"
    assert report["execution"]["affected"] == ["job-1"]
