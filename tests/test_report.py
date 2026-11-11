import csv
import json
import subprocess
import sys

import pytest

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
                "checks": {
                    "target_legality": {"status": "verified"}
                },
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
    assert saved["validation_coverage"]["required_checks"] == ["target_legality"]
    assert saved["validation_coverage"]["scopes"] == {"target_legality": 2}
    assert "semantics" not in saved["validation_coverage"]
    assert "behavior has not been compared" in saved["validation_coverage"]["scope_note"]
    assert "verified native target legality" in before["report.md"].decode()
    assert before["report.md"].decode().index("behavior has not been compared") < before[
        "report.md"
    ].decode().index("| Metric | Score |")
    assert "smoke subset" in before["report.md"].decode()
    with open(paths["cases.csv"], newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert all(row["validation_scope"] == saved["validation_coverage"]["scope_note"] for row in rows)
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
    assert "behavior has not been compared" in report["validation_coverage"]["scope_note"]


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


def test_evolve_failure_is_reported_against_evolve_and_never_dropped():
    from benchpress_compare.report import render_markdown

    plan, observations = fixture()
    observations[1].update(status="COMPILER_FAILED", reason="native_crash", N2=None, D2=None)
    report = build_report(plan, observations, status="COMPLETE_WITH_FAILURES")
    assert report["failures"]["by_arm"] == {"baseline": {}, "evolve": {"COMPILER_FAILED": 1}}
    assert [row["input_id"] for row in report["failures"]["evolve_only"]] == ["Q/circuit"]
    assert report["summary"]["metrics"]["N2"]["score"] is None
    assert report["summary"]["metrics"]["N2"]["circuit_counts"]["unclassifiable"] == 1
    partial = report["score_without_evolve_failures"]
    assert partial["excluded_inputs"] == ["Q/circuit"]
    assert partial["evolve_failed_observations"] == 1
    assert partial["metrics"]["N2"]["score"] is None  # nothing is left in this one-circuit plan
    markdown = render_markdown(report)
    assert "Evolve failed where baseline succeeded: 1 observations" in markdown
    assert "Score without evolve-failed circuits (PARTIAL: leaves out 1 circuits" in markdown


def test_score_without_evolve_failures_covers_the_remaining_circuits():
    plan, observations = fixture()
    other = dict(plan["cases"][0], case_id="d", input_id="Q/other")
    plan["cases"].append(other)
    observations += [
        dict(row, observation_id=row["arm"] + "-d", case_id="d", N2=20, D2=20)
        for row in observations
    ]
    observations[1].update(status="COMPILER_FAILED", N2=None, D2=None)
    report = build_report(plan, observations)
    partial = report["score_without_evolve_failures"]
    assert report["summary"]["metrics"]["N2"]["score"] is None
    assert partial["excluded_inputs"] == ["Q/circuit"]
    assert partial["excluded_weight"] == {"numerator": 1, "denominator": 8}
    assert partial["metrics"]["N2"]["score"] == 1


@pytest.mark.parametrize("name", ["evolve", "small"])
def test_subset_report_preserves_profile_provenance_and_labels_search_signal(name):
    from benchpress_compare.identity import canonical_hash
    from benchpress_compare.profiles import load_profile
    from benchpress_compare.report import render_markdown

    plan, observations = fixture()
    manifest = load_profile(name)
    plan.update(profile=name, design=manifest["design"], profile_manifest=manifest,
                profile_manifest_hash=canonical_hash(manifest))
    report = build_report(plan, observations)
    assert report["profile"] == name
    assert report["profile_manifest_hash"] == canonical_hash(manifest)
    assert report["provenance"]["profile_manifest"] == manifest
    assert report["score_scope"] == "subset_search_signal"
    markdown = render_markdown(report)
    assert manifest["design"] in markdown
    assert "subset score is a search signal" in markdown
    assert "Confirm any improvement on the full" in markdown
    if name == "small":
        assert "no large-band inputs" in markdown


def multi_fixture():
    """Eight single-case circuits, one per family, with spread-out ratios."""
    cases, observations = [], []
    for index in range(8):
        case_id = f"c{index}"
        cases.append(
            {
                "case_id": case_id,
                "input_id": f"Q/circuit{index}",
                "family": f"F{index + 1}",
                "band": "SML"[index % 3],
                "recipe": "R",
                "level": 2,
                "target_class": "square",
                "target_id": "square9",
                "weight": {"numerator": 1, "denominator": 8},
                "sensitivity_weight": {"numerator": 1, "denominator": 8},
                "seeds": [1],
            }
        )
        for arm, n2, d2 in (("baseline", 100, 50), ("evolve", 80 + 5 * index, 40 + 3 * index)):
            observations.append(
                {
                    "observation_id": f"{arm}-{case_id}",
                    "case_id": case_id,
                    "arm": arm,
                    "seed": 1,
                    "status": "SUCCEEDED",
                    "N2": n2,
                    "D2": d2,
                    "validation": {
                        "status": "verified",
                        "checks": {"target_legality": {"status": "verified"}},
                    },
                }
            )
    plan = {
        "schema_version": 1,
        "cases": cases,
        "design": "benchpress-eight-family-v1",
        "plan_hash": "sha",
    }
    return plan, observations


def test_charts_are_written_well_formed_and_referenced_from_markdown(tmp_path):
    import xml.etree.ElementTree as ET

    plan, observations = fixture()
    paths = write_report(tmp_path, build_report(plan, observations, status="COMPLETE"))
    charts = {name for name in paths if name.endswith(".svg")}
    # One circuit is too little for a histogram; the other charts draw.
    assert charts == {
        "charts/slice_change.svg",
        "charts/circuit_outcomes.svg",
        "charts/largest_changes.svg",
    }
    markdown = open(paths["report.md"]).read()
    for name in charts:
        png = name[:-4] + ".png"
        # The PNG is embedded and the SVG linked beside the caption.
        assert f"]({png})" in markdown
        assert f"[SVG]({name})" in markdown
        root = ET.fromstring(open(paths[name]).read())
        assert root.tag.endswith("svg")
        assert open(paths[png], "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    assert "change_spread" not in markdown


def test_every_chart_renders_for_a_multi_circuit_plan_and_stale_charts_are_removed(tmp_path):
    from benchpress_compare.report import CHARTS, build_charts

    plan, observations = multi_fixture()
    report = build_report(plan, observations, status="COMPLETE")
    assert list(build_charts(report)) == [chart.name for chart in CHARTS]
    paths = write_report(tmp_path, report)
    first = {name: open(path, "rb").read() for name, path in paths.items()}
    again = write_report(
        tmp_path, build_report(plan, list(reversed(observations)), status="COMPLETE")
    )
    assert first == {name: open(path, "rb").read() for name, path in again.items()}
    # A later single-circuit report has no histogram, so the stale file disappears,
    # and so does any leftover from a chart that no longer exists under that name.
    (tmp_path / "reports" / "charts" / "renamed_chart.svg").write_text("<svg/>")
    (tmp_path / "reports" / "charts" / "renamed_chart.png").write_bytes(b"")
    single_plan, single_observations = fixture()
    paths = write_report(tmp_path, build_report(single_plan, single_observations))
    assert "charts/change_spread.svg" not in paths
    assert not (tmp_path / "reports" / "charts" / "change_spread.svg").exists()
    assert not (tmp_path / "reports" / "charts" / "renamed_chart.svg").exists()
    assert not (tmp_path / "reports" / "charts" / "renamed_chart.png").exists()
    assert not (tmp_path / "reports" / "charts" / "change_spread.png").exists()
    assert (tmp_path / "reports" / "charts" / "slice_change.svg").exists()
    assert "change_spread" not in open(paths["report.md"]).read()


def test_markdown_leads_with_the_headline_and_keeps_a_fixed_section_order():
    from benchpress_compare.report import render_markdown

    plan, observations = multi_fixture()
    markdown = render_markdown(build_report(plan, observations, status="COMPLETE"))
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Headline",
        "## Coverage and admission",
        "## Slices",
        "## Circuits",
        "## Largest changes",
        "## Failures",
        "## Routing and resource diagnostics",
        "## Reproduction and detailed data",
    ]
    assert markdown.index("| Metric | Score |") < markdown.index("## Coverage and admission")
    assert "| F1 | Fourier, phase estimation and factoring |" in markdown
    assert markdown.index("### N2 improvements") < markdown.index("### D2 improvements")
    assert "| F5 | M | " in markdown  # circuit table rows carry family and band
    assert "`c0`" in markdown  # case ids are shortened, never dropped


def test_svg_helpers_produce_readable_ticks_and_labels():
    from benchpress_compare.report import svg

    assert svg.percent_ticks(-34.5, 11.4) == [-40, -30, -20, -10, 0, 10, 20]
    assert svg.percent_ticks(-2.6, -0.4) == [-3, -2, -1, 0]
    assert svg.count_ticks(287) == [0, 100, 200, 300]
    assert svg.tick_formatter([0.4, 1.0])(1.0) == "1.0"
    assert [svg.percent_label(v) for v in (-30, 0, 10)] == ["-30%", "0%", "+10%"]
    assert [svg.change_label(v) for v in (-2.497, 0, 5.67)] == ["-2.5%", "0.0%", "+5.7%"]
    assert svg.ink_on(svg.SAME) == svg.INK
    assert svg.ink_on(svg.BETTER) == svg.WHITE
    assert svg.shorten("abcdefghijklmnopqrstuvwxyz", 12) == "abcdefg…wxyz"


def test_report_falls_back_to_svg_when_png_rendering_is_unavailable(tmp_path, monkeypatch):
    from benchpress_compare.report import writer
    from benchpress_compare.report.raster import RasterUnavailable

    def unavailable(markup, **kwargs):
        raise RasterUnavailable("no renderer on this host")

    monkeypatch.setattr(writer, "render_png", unavailable)
    plan, observations = fixture()
    paths = write_report(tmp_path, build_report(plan, observations, status="COMPLETE"))
    assert not any(name.endswith(".png") for name in paths)
    markdown = open(paths["report.md"]).read()
    assert "](charts/slice_change.svg)" in markdown
    assert "[SVG]" not in markdown
    assert "PNG versions were not rendered: no renderer on this host." in markdown


def test_png_rendering_uses_only_the_bundled_fonts():
    from benchpress_compare.report import raster

    assert [path.name for path in raster.font_paths()] == list(raster.FONT_FILES)
    assert all(path.is_file() for path in raster.font_paths())
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<text x="2" y="14" font-size="12">A</text></svg>'
    )
    png = raster.render_png(svg, scale=1.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png == raster.render_png(svg, scale=1.0)


def test_single_target_single_level_profiles_skip_redundant_slices():
    """The evolve and small profiles use one target and one level: no repeated rows."""
    from benchpress_compare.report import build_charts, render_markdown

    plan, observations = multi_fixture()
    plan["design"] = "benchpress-small-v1"
    for index, case in enumerate(plan["cases"]):
        case.update(target_class="heavy-hex", target_id="heavy-hex-9", band="SM"[index % 2])
    report = build_report(plan, observations, status="COMPLETE")
    markdown = render_markdown(report)
    assert "no large-band inputs" in markdown
    assert "### By optimization level" not in markdown
    assert "### By target class" not in markdown
    assert (
        "Every case shares one optimization level (level 2) and one target class (heavy-hex)"
        in markdown
    )
    assert "### By width band" in markdown and "### By family" in markdown
    assert "| Family | S | M |" in markdown
    assert "| Family | heavy-hex |" not in markdown
    charts = build_charts(report)
    assert set(charts) == {"slice_change", "circuit_outcomes", "largest_changes", "change_spread"}
    assert "OPTIMIZATION LEVEL" not in charts["slice_change"]
    assert "TARGET CLASS" not in charts["slice_change"]
    assert "WIDTH BAND" in charts["slice_change"] and "CIRCUIT FAMILY" in charts["slice_change"]
