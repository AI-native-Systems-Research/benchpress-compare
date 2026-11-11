# Benchpress Compare

Compare two Qiskit source versions on the same circuits, targets, and random
seeds. The report measures **native two-qubit gate count (N2)** and **two-qubit
depth (D2)** separately. Scores are `evolve / baseline`: below 1 is better.

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), Python
3.12, and two Qiskit source directories containing
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
`../benchpress`.

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

Submit a complete comparison:

```bash
uv run benchpress-compare run \
  --baseline ../qiskit-baseline \
  --evolve ../qiskit-evolve \
  --corpus ../benchpress \
  --store ./store \
  --results-root ./results/lsf-small \
  --profile small \
  --executor lsf --lsf-config lsf-site.toml
```

## Choose a profile

| Profile | Inputs | Targets / optimization levels | Measurements with 3 seeds |
| --- | ---: | --- | ---: |
| `small` | 22 | Heavy-hex / L2 | 132 |
| `evolve` | 44 | Heavy-hex / L2 | 264 |
| `full` (default) | 147 | Square, heavy-hex, FakeTorino / L2 and L3 | 5,292 |

Use `small` to for a short run, `evolve` when using in evolve frameworks like Coral, and `full` to confirm
results. 

## Read results


When collection finishes, open `<results-root>/reports/report.md` for the readable
summary. 

Some failures change which score you get:

- **Baseline fails on an input.** That input is left out of the score. It still
  appears in the report, with zero weight.
- **Evolve fails on an input the baseline completed.** The report shows no main
  score. It shows a partial score instead: one that leaves out the failed
  circuits, labelled with how many it leaves out and how much of the total
  weight they carry.
- **A circuit family ends up with no inputs.** The report shows no score, because
  the missing family's weight is not handed to the other families. To score
  anyway, rerun with `--allow-missing-families`; the report then labels the
  result as a reduced design.

Before trusting a score, check the exclusions and failures listed next to it.


## More detail

- Profile specifications: [small](docs/design/benchpress_subset_small_spec.md),
  [evolve](docs/design/benchpress_subset_evolve_spec.md),
  [full](docs/design/benchpress_subset_spec.md)
- All options: `uv run benchpress-compare run --help`


