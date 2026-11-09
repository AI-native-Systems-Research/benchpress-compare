"""Command-line interface; importing this module never imports Qiskit."""

from __future__ import annotations

import argparse
from dataclasses import fields
import json
from pathlib import Path
import sys

from .config import RunConfig
from .errors import CompareError, ExitCode
from .identity import canonical_hash, read_json


def parser():
    root = argparse.ArgumentParser(prog="benchpress-compare", description=__doc__)
    root.add_argument("--version", action="version", version="benchpress-compare 0.1.0")
    commands = root.add_subparsers(dest="command", required=True,
                                  metavar="{run,prepare,resume,status,report,cancel,export,audit}")
    for name in ("run", "prepare", "resume", "_controller"):
        command = commands.add_parser(name, **({} if name == "_controller" else {"help": name.capitalize() + " a comparison"}))
        command.add_argument("--results-root", required=True)
        if name in {"run", "prepare"}:
            for path in ("baseline", "evolve", "store", "corpus", "lsf-config"):
                command.add_argument("--" + path)
            command.add_argument("--executor", choices=("local", "lsf"))
            command.add_argument("--seeds", type=int)
            command.add_argument("--master-seed", type=int)
            command.add_argument("--workers")
            command.add_argument("--native-threads", type=int)
            command.add_argument("--cpu-budget", type=float)
            command.add_argument("--memory-gb", type=float)
            command.add_argument("--wall-seconds", type=float)
            command.add_argument("--worker-memory-gb", type=float)
            command.add_argument("--infrastructure-retries", type=int)
            command.add_argument("--smoke", action="append", metavar="INPUT_ID")
            command.add_argument("--accept-unresolved", action="store_true", default=None)
        if name in {"run", "prepare", "resume"}:
            command.add_argument("--wait", action="store_true")
        if name in {"resume", "_controller"}:
            command.add_argument("--retry-cancelled", action="store_true")
        if name in {"resume", "_controller"}:
            command.add_argument("--accept-unresolved", action="store_true", default=None)
        if name == "_controller":
            command.add_argument("--prepare-only", action="store_true")
    for name in ("status", "cancel", "export", "report"):
        command = commands.add_parser(name)
        command.add_argument("--results-root", required=True)
        if name == "export":
            command.add_argument("--output", required=True)
        elif name == "report":
            for dimension in ("circuit", "target", "target-snapshot", "family", "band", "recipe"):
                command.add_argument("--" + dimension, action="append")
            command.add_argument("--level", action="append", type=int, choices=(2, 3))
            command.add_argument("--group-by", action="append", choices=("circuit", "target", "level", "family", "band", "recipe"))
    audit = commands.add_parser("audit", help="Audit the checked-in catalogue and optionally a pinned corpus mirror")
    audit.add_argument("--corpus")
    return root


def _config(args):
    path = Path(args.results_root).expanduser().resolve()
    request = path / "request.json"
    if request.exists():
        config = RunConfig(**read_json(request)["config"])
        if str(path) != config.results_root:
            raise CompareError("Results directory moved; use export metadata to recreate environments and references")
        for field in fields(RunConfig):
            supplied = getattr(args, field.name, None)
            if supplied is None or field.name == "results_root":
                continue
            saved = getattr(config, field.name)
            if field.name == "accept_unresolved" and supplied is True:
                config.accept_unresolved = True
                continue
            if field.name in {"baseline", "evolve", "store", "corpus", "lsf_config"}:
                supplied = str(Path(supplied).expanduser().resolve())
            if supplied != saved:
                raise CompareError(f"Saved {field.name} is immutable; use a new results directory")
        return config
    if args.command in {"resume", "_controller"}:
        raise CompareError("No saved request to resume")
    required = ("baseline", "evolve", "store")
    if any(getattr(args, key, None) is None for key in required):
        raise CompareError("A new run requires --baseline, --evolve, --store, and --results-root")
    values = {field.name: getattr(args, field.name) for field in fields(RunConfig)
              if getattr(args, field.name, None) is not None}
    return RunConfig(**values).validate()


def _json(value):
    print(json.dumps(value, sort_keys=True, indent=2, allow_nan=False))


def _exit_status(state):
    return {"COMPLETE_WITH_FAILURES": ExitCode.COLLECTION, "PREPARATION_FAILED": ExitCode.PREPARATION,
            "NEEDS_RECONCILIATION": ExitCode.RECONCILIATION, "INTERRUPTED": ExitCode.INTERRUPTED,
            "CANCELLED": ExitCode.COLLECTION}.get(state, ExitCode.SUCCESS)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "audit":
            from .corpus import load_catalogue, verify_corpus
            from .weights import allocate_weights
            catalogue = load_catalogue()
            main_candidates = [row for row in catalogue["candidates"] if row["category"] == "main"]
            weights = allocate_weights(main_candidates)
            output = {"schema_version": 1, "candidate_count": len(catalogue["candidates"]),
                "main_candidates": len(main_candidates), "coverage_count": len(catalogue["coverage"]),
                "catalogue_hash": canonical_hash(catalogue), "revision": catalogue["revision"],
                "expected_preflight_observations": 149 * 3 * 2 * 3, "planning_weights": weights["marginals"]}
            if args.corpus:
                output["corpus"] = verify_corpus(args.corpus, catalogue)
            _json(output)
        elif args.command == "status":
            from .orchestrator import status
            _json(status(args.results_root))
        elif args.command == "cancel":
            from .orchestrator import cancel
            _json(cancel(args.results_root))
        elif args.command == "export":
            from .export import export_run
            _json(export_run(args.results_root, args.output))
        elif args.command == "report":
            from .orchestrator import read_results, status, verify_plan
            from .report import build_report, write_report
            plan = verify_plan(args.results_root, check_harness=False)
            query = {key: getattr(args, key) for key in ("circuit", "target", "target_snapshot", "family", "band", "recipe", "level")
                     if getattr(args, key)}
            paths = write_report(args.results_root, build_report(plan, read_results(args.results_root),
                                query=query, status=status(args.results_root), group_by=args.group_by))
            _json({key: str(value) for key, value in paths.items()})
        else:
            config = _config(args)
            prepare_only = args.command == "prepare" or getattr(args, "prepare_only", False)
            if config.executor == "lsf" and args.command != "_controller":
                from .controller import follow_controller, submit_controller
                job = submit_controller(config, prepare_only=prepare_only,
                                        retry_cancelled=getattr(args, "retry_cancelled", False))
                _json({"controller_job_id": job.get("job_id"), "status": job["status"], "results_root": config.results_root})
                if args.wait:
                    return int(_exit_status(follow_controller(config, job, prepare_only=prepare_only)))
                return 0
            from .preparation import prepare
            from .orchestrator import collect
            plan = prepare(config, continue_existing=True,
                           retry_cancelled=getattr(args, "retry_cancelled", False))
            if prepare_only:
                _json({"status": "FROZEN", "plan_hash": plan["plan_hash"], "cases": len(plan["cases"])})
            else:
                terminal = collect(config.results_root, retry_cancelled=getattr(args, "retry_cancelled", False))
                _json({"status": terminal, "results_root": config.results_root})
                return int(_exit_status(terminal))
        return 0
    except CompareError as exc:
        print(f"benchpress-compare: {exc}", file=sys.stderr)
        return int(exc.exit_code)
    except (OSError, ValueError) as exc:
        print(f"benchpress-compare: {exc}", file=sys.stderr)
        return int(ExitCode.PREPARATION if args.command in {"run", "prepare", "resume", "_controller"} else ExitCode.CONFIGURATION)
    except KeyboardInterrupt:
        print("Interrupted; committed results are preserved. An LSF --wait client detaches without cancelling jobs.", file=sys.stderr)
        return int(ExitCode.INTERRUPTED)


if __name__ == "__main__":
    raise SystemExit(main())
