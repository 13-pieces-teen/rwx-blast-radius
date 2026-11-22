# v11: import-state changes and attribute consumers

The runtime compares the imported SQLGlot module globals and defining class fields against a pristine snapshot from the same production environment. Changes propagate through concrete field readers, inheritance, aliases and derived values. A line executed during import no longer automatically selects the entire suite. Class declaration changes use the fields whose values actually changed, rather than every consumer of the class.

The selector retains runtime line/condition coverage, source readers, reflection, structural dependencies and the shared-executor repair. Existing precise lambda-call dependencies remain active; changing a lambda inside a dispatch table does not select every reader of the entire table. When observed attribute readers are unavailable, same-name access coverage is restricted to consumers of the actual receiver and applicable derived classes.

The production image is `sha256:3a370f17d56ad9d990c62648a40f42377a8e779ce6e98bf1aba501d34210c2b8`. A build-time pristine import verifies zero differences against the packaged snapshot. The final Linux regression run passed **186 test methods**, including the existing actual-execution subcases.

## Official practice result

Commit `7261e88b8ff80fb7faefca472c79596e08c9387e`, receipt **BB-50143916**, scored **189.3 seconds** on 2026-11-22. All three runs missed zero commits, with simulated CI of 178.068 seconds and container CPU of 11.20 / 11.46 / 10.92 seconds. The page automatically entered this submission in the final as the team's best score.

Against the previous best v8 score of 214.3 seconds, the displayed improvement is **25.0 seconds (11.7%)**. Simulated CI falls by 33.909 seconds while the displayed median CPU rises by 8.84 seconds. The two main mechanisms therefore provide a net official practice improvement after paying their analysis cost. This is not a measurement of the unseen final batch. [Official receipt and measurements](official-practice-v11-2026-11-22.json).

## Directed integration batch

Twenty existing patches with complete native truth were selected by syntax, source location and a fixed hash order, without filtering on failures or estimated savings. All original inputs and prior unsuccessful candidate runs are retained locally.

| Same 20 inputs | Simulated CI seconds | Container CPU seconds | CI plus CPU seconds | Missed commits |
| --- | ---: | ---: | ---: | ---: |
| State optimization off (v10-equivalent selections) | 156.836 | 3.122968 | 159.958968 | 0 |
| Final v11 candidate | 88.392 | 16.791058 | 105.183058 | 0 |

The candidate changes eight selections and saves **54.775910 seconds** after its additional CPU cost in this single-run comparison. This is a directed integration batch, not an estimate of the hidden competition score or a three-run median.

## Implementation and evidence

All **806 patches** completed successfully across 21 deterministic groups, with no OOM or runtime rejection. Against v10, 159 selections changed. All 261 cases with complete native truth and all 30 supplied public labels retain every failing test; the observed partial records introduce no known missed failures. The remaining 515 cases do not have complete truth, and 68 of them have newly omitted tests. They are not counted as passing validation.

| Cohort | Patches | v10 CI seconds | v11 CI seconds | v11 CPU seconds, partitioned sum |
| --- | ---: | ---: | ---: | ---: |
| Development | 400 | 3969.441 | 3424.265 | 238.894499 |
| Independent holdout | 160 | 1775.241 | 1522.056 | 109.708140 |
| Targeted | 40 | 689.078 | 518.343 | 33.583652 |
| Public | 30 | 117.861 | 112.487 | 5.846226 |
| Historical | 156 | 1819.682 | 1623.753 | 92.575265 |
| Reserve | 20 | 182.013 | 172.757 | 8.535201 |

These differently sampled cohorts are not a hidden-score forecast. Their total CI reduction is 1179.655 seconds, before comparing CPU. The candidate CPU sum is 489.142983 seconds across 21 processes; the older v10 run used six processes, so no exact paired net improvement is inferred from those two series.

The final public30 three-run check has identical workers and CI of **112.487 seconds**. Container CPU is **6.451886 / 5.999624 / 6.157728 seconds**, giving a CPU-inclusive median of **118.644728 seconds**. All supplied public failures are retained in every run. This is a local sample result, separate from official scoring.

The state profile contains 24,775 fields across 183 modules and 1,296 classes. Baseline artifacts come only from pinned, unmodified SQLGlot source and its coverage observations. Mutation patches, failure labels and expected selections are excluded from the runtime image.

The first integration run exposed a real environment mismatch: the baseline collector image contained optional type-helper dependencies absent from production. Baselines were then regenerated in the production dependency environment. A type tuple constructed from a set also varied by class address; a generic source-based rule canonicalizes such tuples only when their uses are type-membership checks. Ordinary ordered tuples and class hierarchy order remain significant. The build-time no-change check now detects environment drift.

The aligned first candidate exposed two precision regressions, both corrected before the final image: unrelated classes sharing an attribute name were conflated, and existing lambda-call precision was replaced by whole-table consumers. These were attribution errors, not reasons to disable the state-difference mechanisms.

Implementation boundaries and profile provenance are documented in [import state](import-state.md) and [field readers](import-state-readers.md). Native mutation outcomes validate the fixed-suite behavior empirically; the snapshot is not a proof about arbitrary interpreter or external side effects.

Local records: `evaluation/v11/runs/fast20-off-control`, `fast20-final`, and the full validation under `readers-806-final`; the consolidated report is `evaluation/v11/verification/FINAL-VERIFICATION.json`. Complete batches are split into deterministic groups of at most 40 patches to match practice-batch scale. Container CPU sums include repeated startup per group and must not be presented as directly paired with older unpartitioned CPU measurements. Official scoring is recorded separately above.
