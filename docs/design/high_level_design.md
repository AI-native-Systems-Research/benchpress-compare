# Benchpress comparison: high-level design

This tool compares two Qiskit source folders and shows whether the changed version
produces better circuits. It reports two measures: **N2**, the number of native
two-qubit gates, and **D2**, the number of sequential layers those gates require.
Lower is better for both.
A change can improve one measure and worsen the other, so each gets its own score.

This is a proposed design based on the [requirements](requirements.md). The
[subset specification](benchpress_subset_spec.md) defines which circuits to consider,
their targets, and their contribution to the score.

**A run starts with four paths.**

| Input | Purpose |
| --- | --- |
| `baseline` | The Qiskit source folder used as the reference. |
| `evolve` | The Qiskit source folder containing the changes to evaluate. |
| `store` | A shared location for baseline builds and results that can be reused across comparisons. |
| `results-root` | A separate folder for this comparison's plan, progress, results, and report. |

The user also chooses local or LSF execution, the number of seeds per case
(default: **3**), and resource limits. One **orchestrator**, the process managing the
run, prepares the work, assigns it to workers, tracks completion, and produces the
report. Local and cluster execution use the same comparison rules.

**First, prepare a fair comparison.** Copy both source versions into isolated build
environments and compile them in **release mode**, with native compiler optimizations
enabled. Record the source contents and build settings so the results can be traced
back to exactly what ran. A cached build is usable only if it matches these requirements.

Next, run the subset's eligibility checks using baseline only. These checks use three
preflight seeds to establish which circuits qualify on all three targets at both L2
and L3. Save the admitted circuits, targets, settings, and recalculated weights before
measuring evolve. This saved plan stays fixed throughout the comparison, including
retries and resumed runs.

**Decide how much work to run.** A *case* means one circuit on one target at one
optimization level. For example, the 29-qubit QFT on the square target at L2 is one
case. A *seed* controls the randomized choices during transpilation. Repeating a case
with different seeds shows how much its result varies.

The number of comparison seeds is a user input, with a default of **3 per case**.
Use that count for every case in both builds. A short baseline pilot estimates runtime
and memory use to help choose worker counts and LSF job sizes for the requested work.

Both builds use the same chosen seed list for each case. Three seeds therefore mean
six transpilation runs: three for baseline and three for evolve. Save the seed lists
before measuring evolve, and keep them separate from the eligibility seeds. Additional
seeds improve the estimate without increasing the case's weight in the score.

**Run locally or on LSF.** A local worker is a process that executes assigned
transpilation runs. Choose the worker count from available CPU and memory, using the
pilot's memory estimates. Allow the user to override it, and control native thread
counts so workers do not compete for more CPU than the machine has.

On LSF, divide the same work into jobs. Use the pilot to recommend job count, memory,
and time limits. Group short cases together and give expensive cases their own jobs
when needed. Keep each case's baseline and evolve runs on matching resources.

The orchestrator runs as an LSF job, submits work up to the configured concurrency
limit, and polls the scheduler until every job succeeds or fails. It saves job IDs,
attempts, and completed results as it goes. After an interruption, it checks existing
jobs and resumes unfinished work. Infrastructure failures can be retried a limited
number of times with the same seeds; compiler failures remain visible. Successful
results are kept, so resuming does not repeat the whole benchmark.

**Read the results at the level that matters.** The report shows overall D2 and N2
scores, then lets the reader examine a *slice*: a selected group of results. Examples
include one circuit, L3 only, one target, one family, one size band, or large arithmetic
circuits at L3. Every slice shows both scores and separate better/same/worse circuit
counts for each measure.

For one case, average each build's measurements across seeds, then divide evolve by
baseline. For example:

| Measure | Baseline average | Evolve average | Ratio | Meaning |
| --- | ---: | ---: | ---: | --- |
| N2 | 1,000 | 900 | 0.90 | 10% fewer two-qubit gates |
| D2 | 200 | 210 | 1.05 | 5% greater two-qubit depth |

Combine case ratios using the subset's weighted geometric mean. The weights prevent
families with many circuit files from dominating the result. For a slice, use the
same weights restricted to that slice and scaled to sum to one.

For better/same/worse counts, first combine each circuit's cases within the slice
using those weights, then count that circuit once. A ratio below, equal to, or above
one determines its category. Thus a slice with 20 circuits could show 12 better,
5 unchanged, and 3 worse for N2, with different counts for D2. Missing or invalid
results get their own count. They block the affected complete score; zero values
also need explicit reporting because the logarithmic score requires positive ratios.

**Keep reusable work separate from each comparison.** Following the existing
[qtb store model](../../../qiskit-transpile-bench/README.md), `store` holds reusable
baseline work. Build reuse depends on source and build environment; result reuse
also depends on the circuit, target, settings, and seed. Only complete entries become
available to other runs sharing the store.

`results-root` holds this run's evolved build, saved plan, job history, raw measurements,
logs, and Markdown and machine-readable reports. Comparing another change against
the same baseline uses the same store and a new results folder. Resuming an interrupted
comparison uses its existing folder and unchanged plan.

Implementation can reuse the build and storage patterns in
[qtb](../../../qiskit-transpile-bench/src/qtb/) and the cluster management patterns in
[bpkit](../../../bpkit/bpkit/).
