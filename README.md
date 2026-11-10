# Benchpress Compare

Compare two Qiskit source versions on the same circuits, targets, and random
seeds. The report measures **native two-qubit gate count (N2)** and **two-qubit
depth (D2)** separately. Scores are `evolve / baseline`: below 1 is better.

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), Python
3.12 (pinned to 3.12.12), and two Qiskit source directories containing
`pyproject.toml`. You also need the Rust/Cargo and native compiler toolchain
required by those Qiskit versions. The tool builds each version in its own
environment; you do not need to install Qiskit yourself.

Run these commands from this repository. Replace the source paths with yours.

### 1. Install

```bash
uv sync --locked
```

### 2. Download BenchPress

[Qiskit BenchPress](https://github.com/Qiskit/benchpress) supplies the benchmark
circuits and generator recipes. **Its assets must be downloaded separately;
they are not included in this repository.** Use this exact revision:

```bash
git clone https://github.com/Qiskit/benchpress.git ../benchpress
git -C ../benchpress checkout 4c04b38a53fc231a89f1a6bc9541c1b641ed6dcf
```

**`--corpus` is the path to that BenchPress checkout's root directory**, such as
`../benchpress`. It is not a Qiskit source directory, an individual circuit file,
or the nested `benchpress/` package directory. The tool checks the revision and
asset hashes before using it. An extracted archive of the same revision also
works.

### 3. Run locally

Start with the small profile:

```bash
uv run benchpress-compare run \
  --baseline ../qiskit-baseline \
  --evolve ../qiskit-evolve \
  --corpus ../benchpress \
  --store ./benchmark-store \
  --results-root ./benchmark-results/local-small \
  --profile small
```

`run` builds both Qiskit versions, freezes the benchmark plan, collects results,
and writes reports. No separate `prepare` command is needed.

| Option | Meaning |
| --- | --- |
| `--baseline` | Qiskit source directory used as the reference. |
| `--evolve` | Qiskit source directory with the changes you want to evaluate. |
| `--corpus` | Local BenchPress checkout or extracted archive. |
| `--store` | Shared cache for baseline builds and successful baseline measurements; reuse it across runs. |
| `--results-root` | Output directory for this comparison; use a new one for each change or profile. |

The store and results directory must be separate and outside both Qiskit source
trees. For an A/A check, pass the same source directory to both `--baseline` and
`--evolve`; both sides still execute independently.

## Run on LSF

Submit a complete comparison, replacing `/shared/...` with your shared paths:

```bash
uv run benchpress-compare run \
  --baseline /shared/sources/qiskit-baseline \
  --evolve /shared/sources/qiskit-evolve \
  --corpus /shared/sources/benchpress \
  --store /shared/benchmarks/store \
  --results-root /shared/benchmarks/results/lsf-small \
  --profile small \
  --executor lsf --lsf-config lsf-site.toml
```

## Choose a profile

| Profile | Inputs | Targets / optimization levels | Measurements with 3 seeds |
| --- | ---: | --- | ---: |
| `small` | 22 | Heavy-hex / L2 | 132 |
| `evolve` | 44 | Heavy-hex / L2 | 264 |
| `full` (default) | 149 | Square, heavy-hex, FakeTorino / L2 and L3 | 5,364 |

Use `small` to screen changes, `evolve` for a larger check, and `full` to confirm
results. Each keeps all eight circuit families. Counts are before baseline
admission. Defaults are 3 seeds, 1 native thread, and 8 GB per worker. Per
measurement time limits are 60 seconds for small, 900 for evolve, and 3,600 for
full; override with `--wall-seconds`. Small still builds both Qiskit versions.

## Check progress and read results

Use your run's results path in these commands (the local example is shown):

```bash
uv run benchpress-compare status --results-root ./benchmark-results/local-small
uv run benchpress-compare resume --results-root ./benchmark-results/local-small
```

`resume` uses the saved configuration and keeps successful measurements. Source
edits do not change an existing run; use a new results directory to test them.
To stop a run, use `cancel --results-root <path>`. Restart explicitly cancelled
work with `resume --results-root <path> --retry-cancelled`.

When collection finishes, open `<results-root>/reports/report.md` for the readable
summary. The same directory contains `summary.json`, `cases.csv`, and
`observations.csv`. You can regenerate reports with
`uv run benchpress-compare report --results-root <path>` without Qiskit installed.

Inputs with failed or missing baseline measurements are excluded. An evolve
failure on an admitted input makes the main score unavailable; the report also
shows a labelled partial score. An empty circuit family also blocks the standard
score. Read exclusions and failures alongside the scores.

Accepted outputs are checked for native target compatibility. Semantic
equivalence with the input circuit is not checked, so these scores alone do not
establish compiler correctness.

## More detail

- [Benchmark flow and result layout](docs/benchmark_flow.md)
- [Validation coverage and remaining checks](docs/validation_coverage.md)
- Profile specifications: [small](docs/design/benchpress_subset_small_spec.md),
  [evolve](docs/design/benchpress_subset_evolve_spec.md),
  [full](docs/design/benchpress_subset_spec.md)
- All options: `uv run benchpress-compare run --help`

For development checks, run `uv run --locked pytest` and
`uv run --locked ruff check src tests`. Tests requiring the pinned Qiskit/provider
environment use `uv run --locked --group integration pytest`.

Adapted BenchPress recipes retain their
[Apache 2.0 license](data/LICENSE-BENCHPRESS.txt).
