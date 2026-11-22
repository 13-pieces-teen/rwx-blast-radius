# v12: default-value callers and expression-level coverage

This experiment starts from v11's submitted 189.3-second version. It targets the remaining default-argument and multiline-condition over-selection identified in the earlier diagnosis. Production selection uses pinned source, baseline coverage and parameter observations; no mutation IDs, expected failures or patch-to-answer tables enter the image.

## Official practice result

Commit `fec3d59600d5d8cd8f4247e4eade2b90a7d7d845`, receipt **BB-9A4FE89D**, scored **188.8 seconds** on 2026-11-22 and is automatically entered in the final as the team's best score. All three runs missed zero commits. Each run had 178.068 simulated seconds, with CPU of 10.85 / 10.54 / 10.70 seconds. [Official result](official-practice-v12-2026-11-22.json).

This is only **0.5 seconds better than v11**. The official simulated total is unchanged, despite the local 296.461-second reduction across the 806 cases. The observed improvement is the displayed CPU median difference. Hidden per-patch outputs are unavailable, so this does not establish identical selections or isolate which inputs account for the aggregate. It also does not isolate the transport change's causal CPU benefit. The experiment validates broader local selection precision but does not demonstrate the intended large official reduction.

## Default-value dependencies

The old default rule rejected every nonempty affected set through its persistent-state guard. In addition, v11's outer import-state layer could replace its result with whole-function coverage and generic readers of the changed function object. The new path composes these analyses explicitly.

For one immutable literal default replacement, function-body coverage supplies the initial dependency. Existing boolean parameter observations retain possible-default and opaque calls, while excluding observed callers that always supplied the opposite boolean explicitly. Unobserved body coverage and metadata readers remain included. Other literal defaults use body coverage and observed metadata-inspection sites. Integer, None, string and immutable tuple replacements are supported; generators, async functions, nested functions and decorated definitions are not rejected solely because of their form.

A fresh import-state comparison still captures changed derived values. For ordinary function roots and direct callable aliases, call/metadata dependencies replace generic class or attribute readers. The traversal continues through aliases, inheritance and test-declaration caches, and other changed fields keep their own readers. Unknown decorator wrappers and nested exported callables retain their state dependencies. Source, reflection and structural dependencies remain independent unions. Shared-executor propagation runs last.

The optional `can_skip_snapshot` observation is diagnostic in this version; the runtime still performs the snapshot. No global persistent-state proof is required before selecting an affected call. As with ordinary baseline coverage, cross-test behavior is checked empirically against the fixed suite and its known shared-executor dependencies.

## Expression dependencies

The selector finds the smallest aligned AST expression containing a semantic edit. An actual bytecode load of its first necessarily evaluated operand supplies the baseline execution-line anchor. This avoids unioning every line of an enclosing multiline condition when only a later short-circuit operand changed. Operator changes that affect the entire boolean expression retain its initial evaluation anchor.

The same expression plan is used by ordinary line selection and import-state runtime selection. Collection relevance is checked at that anchor, so an unexecuted child expression does not trigger a snapshot merely because another operand ran during import. Actual collection execution still uses v11's state difference. Line relocation, missing compiler anchors, deferred expressions and binding changes retain existing handling. Bytecode indexes are lazy, cached per file and linear in instruction/line-interval count.

## Snapshot transport

The child continues to import and normalize the complete state. It sends a lossless delta to the parent, which reconstructs the full snapshot for existing readers. Added/removed fields, record metadata, aliases, class hierarchy and top-level metadata survive the round trip. This reduces repeated serialization of the approximately 9 MB unchanged snapshot without changing state semantics or baseline artifacts.

## Validation

The immutable candidate image is `sha256:a8491098b9318b172e0b33070b37c47858cc6e4fa7a6b01e3ed905252a0e02b6`. Its build-time pristine import matches the shipped baseline state.

All 219 Linux regression methods are covered: 214 pass in the existing precision image, and five collection tests skipped for missing pytest/coverage pass separately in the mapping image. The original 30 actual-execution subcases also pass. New checks include observed boolean bindings, literal default types, metadata aliases, class fixtures, import-derived state, cached test defaults, short-circuit execution anchors and lossless snapshot transport.

An initial host-only test fixture stalled while importing asyncio under the observer. Both affected test processes were stopped. The async regression now executes the coroutine directly, testing the changed default without loading an event loop inside instrumentation; the corrected ten default tests pass. This did not involve a candidate scoring run.

The directed syntax batch contains all 51 default-value edits and 195 short-circuit edits found in the existing 806 inputs. It was selected by AST alone before candidate execution. All seven batches completed normally. There are 54 changed selections, no missed failures in the 112 complete-label cases, and four unknown cases with newly omitted tests. Candidate CI is 2523.915 seconds versus v11's 2820.370, saving 296.455 seconds before CPU. Partitioned candidate CPU is 143.933074 seconds; this batch has no same-session v11 CPU control.

| Frozen syntax group | Cases | Changed selections | v11 CI seconds | v12 CI seconds | CI reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| Default values | 51 | 37 | 896.712 | 665.735 | 230.977 |
| Short-circuit expressions | 195 | 17 | 1923.658 | 1858.180 | 65.478 |

Across these groups, the candidate omits `test_execute_tpcds` on ten additional patches and `test_max_nodes` on four; these are consequences of source/call dependencies, not test-specific exclusions. The 46 complete default cases and 66 complete short-circuit cases retain all failures. The syntax census is not a hidden-distribution estimate.

The full comparison reuses the identical 806 inputs and v11's 21 fixed partitions. All 21 batches completed normally. There are 55 changed selections, including 54 strict subsets; no known failed test is newly omitted. Complete truth comprises 261 native cases and 30 supplied public cases; the other 515 cases remain explicitly unknown. Five unknown cases have additional omissions, of which only m0314 has a substantial CI reduction (29.034 seconds); the others save zero or 0.006 seconds.

| Cohort | Patches | v11 CI seconds | v12 CI seconds | CI reduction |
| --- | ---: | ---: | ---: | ---: |
| Development | 400 | 3424.265 | 3346.975 | 77.290 |
| Independent holdout | 160 | 1522.056 | 1442.082 | 79.974 |
| Targeted | 40 | 518.343 | 396.547 | 121.796 |
| Public | 30 | 112.487 | 112.487 | 0 |
| Historical | 156 | 1623.753 | 1606.352 | 17.401 |
| Reserve | 20 | 172.757 | 172.757 | 0 |

Total simulated CI falls from 7373.661 to **7077.200 seconds**, a **296.461-second** reduction before CPU. Candidate CPU summed across 21 partitions is **515.093124 seconds**. These are local, differently sampled cohorts; their aggregate is not a forecast of the hidden distribution. Historical CPU sessions are not spliced into an exact paired net-score claim.

The final public30 runs produce identical workers and CI of 112.487 seconds. Container CPU is **5.739057 / 5.710051 / 5.724409 seconds**; the CPU-inclusive median is **118.211409 seconds**. All public failures remain selected. Fast246 workers exactly match the corresponding full806 outputs. All 31 selector containers (7 fast, 21 full, 3 public) exited and cleaned up normally.

The full test suite for m0314 was additionally observed once after selector timing completed, with the original native process pool. It completed in 105.078 seconds without OOM. All 78 failing parent tests are represented in the official test list and retained by the candidate's 90-test selection (v11 selected 1561). CI falls from 36.817 to 7.783 seconds. Its patch and selected outputs were frozen before the result; the original 806 registries remain unchanged. Together with the original 291 complete-label cases, this supplies 292 complete observations, with four other newly narrowed cases still unknown. The independent result is recorded under `evaluation/v12/verification/m0314-truth-result.json`.

Local evidence is under `evaluation/v12/build`, `evaluation/v12/verification` and `evaluation/v12/runs`. Public-sample CPU medians and official practice scoring are separate measurements.
