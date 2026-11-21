# Blast Radius: coverage-guided test selection

An entry for the RWX Buildathon track. For each independent SQLGlot patch, the selector identifies affected tests and chooses a worker count that minimizes recorded CI time plus worker overhead.

## Official practice result

Submission **BB-F47DED7A**, commit `a28c3f227d4cad0effcb037e0162176c1407421c`, was scored on 2026-11-21: **214.4 seconds**. All three runs missed **zero commits**; each had 211.977 simulated seconds and 2.42, 2.44 and 2.44 CPU seconds. It is the team's current best and selected final entry. [Official v7 result](docs/official-practice-v7-2026-11-21.json).

Compared with v6 (`ccec7a6`, 248.4 seconds, also zero-miss), the total decreases by **34.0 seconds (13.69%)**, including **33.430 seconds** less simulated CI time. The original `5887d18` scored 471.7 seconds with one missed commit and 300 penalty seconds per run. Its hypothetical penalty-free total is about 171.7 seconds, but it did not achieve that as a zero-miss entry. The current non-penalty simulated time remains 43.527 seconds above that original entry. Practice results do not guarantee unseen final performance. [Previous v6 result](docs/official-practice-v6-2026-11-21.json).

## Local optimization round v8

Two necessary-condition guards skip analyses which cannot accept the patch. The 156 historical complete selected-test lists and worker assignments remain identical to v7, and the exact frozen candidate passes all 124 Linux regressions. Three constrained public-sample runs remain zero-miss at **117.861 simulated seconds**, with a **119.295208-second** CPU-inclusive median. Final-image interleaved CPU medians are **1.535284 / 1.393933 seconds** for v7/v8. This is a small local CPU improvement; dictionary-entry research remains outside the submission. [Evidence and limits](docs/local-optimization-v8.md).

## Local optimization round v7

Full-prefix tracing removes isolated-retry import noise while retaining original and historical dependency edges. Namespace profiles distinguish key existence from value reads; complete baseline operand observations prove a narrow class of comparison edits unchanged across all observed inputs. Fresh fork children reduce import-check CPU overhead.

The public sample is zero-miss at **117.861 simulated seconds**, saving **9.222 seconds** against v6. The final image's consecutive three-run CPU-inclusive median is **122.110 seconds**; interleaved v6/v7 controls are **129.370 / 119.548 seconds**. Both measurement series are retained because CPU varies. All **124 Linux regression checks** pass. Final omitted sets have direct passing evidence for 27 cases and exact inherited coverage for 28 historical cases; the first extra historical timeout and its follow-up controls are preserved. [Evidence, provenance and limits](docs/local-optimization-v7.md) · [Machine results](docs/local-v7-results.json).

## Local robustness round v6

Class-shared fixture reads and values imported by alias exposed two further omission mechanisms. The collector now wraps class fixtures before pytest captures them, and module declaration changes retain indirect consumers, including EOF insertions. Namespace observations safely distinguish individual mapping keys while preserving opaque consumers and derived class state.

The public sample remains zero-miss in all three constrained runs: **127.083 simulated seconds**, **128.908 seconds CPU-inclusive median** (v5: 130.441). Eight targeted namespace mutations passed all **12,478 omitted tests**; the class-fixture counterexample passed another **1,559**. All **93 regression checks** passed, and final selections preserve 28 completed validation records. These local results remain separate from the subsequent zero-miss official practice result above; the old missed patch itself is not disclosed. [Results, provenance and limits](docs/local-optimization-v6.md).

## Local robustness round v5

Four independently reproduced omission mechanisms are fixed: cross-test dispatch caches, lazy-import derived state, dictionary keys sharing a line with a lambda, and function-wide effects of local bindings. The four diagnosed regression patches improve from 4 misses to 0; their simulated cost including penalties falls from 1210.250 to 81.610 seconds. This deliberately selected regression set is not an unbiased benchmark or an official score.

Twelve new seeded mutations passed all 17,298 omitted-test executions. The final code passes 66 regression checks. On the public sample, stronger coverage costs 8.110 additional simulated seconds; the three-run CPU-inclusive median is 130.441 seconds. This version remains local: no publication or official resubmission was performed in this round. [Validation details and limits](docs/local-optimization-v5.md).

## Measured progress

On the **30 public sample patches**, with the official Docker runtime restrictions:

| Version | Simulated seconds | Median CPU-inclusive total, 3 runs | Missed commits |
| --- | ---: | ---: | ---: |
| Official starter, all tests / 3 workers | 1210.470 | 1210.600 | 0 |
| Coverage selector v1.1 | 183.705 | 187.923 | 0 |
| Attribute-aware selector v2 | 170.654 | 173.871 | 0 |
| Baseline-type equivalence v3 | 120.413 | 123.417 | 0 |
| Cached Python bytecode v3.1 | 120.413 | 121.964 | 0 |
| Field-aware reflection dependencies v4 | 120.413 | 121.797 | 0 |
| Cache/import/scope-safe selector v5 | 128.523 | 130.441 | 0 |
| Shared-fixture and namespace-aware selector v6 | 127.083 | 128.908 | 0 |
| Prefix recovery, namespace keys and comparison observations v7 | 117.861 | 122.110 | 0 |

The v4 result was an 89.94% reduction on the local sample set; v5 intentionally spends more on coverage after reproducing real omissions. These are **local measurements**, not a practice-board or final score. The website's 1614.1-second baseline uses a different evaluation batch and is not the denominator here. CPU is measured as Linux process user+system time, including children; the server uses its container-wide accounting. Small CPU differences between adjacent versions can be measurement noise.

An independently generated 30-mutation batch exposed one collection-time dependency bug, now conservatively handled. All other omitted-test checks passed; v2's additional omissions were also exercised. One generated mutation accidentally matches a sample, so this is not a strict 30-example held-out set. Sample success is not a guarantee for unseen changes. See [PROGRESS.md](PROGRESS.md).

## Approach

- The maps are generated solely from SQLGlot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, before applying any patch. They contain per-test line coverage, fixture reads, expression/dialect class usage and class-configuration reads.
- Warm and per-test cold dispatch-cache traces are unioned. A separate eager-import trace retains state materialized when a module would otherwise be lazily imported by the first test.
- Lambda body selection uses actual calls from complete warm/cold suites and eager imports, keeping dictionary keys, defaults, nested lambda factories and lazy generator expressions conservative. Source-reading and code-reflection dependencies remain active.
- Pure literal class-field changes use observed readers only when the declaration is otherwise unchanged and the field was not read during collection. Collection-time reads retain the broader class dependencies because they can generate cached state.
- A separate baseline trace records operand types at identity comparisons. At runtime, the selector proves selected identity/equality replacements equivalent for the observed built-in slots, exact NoneType and unique enum members. It rejects custom equality, unknown observations and files containing additional AST changes. The trace evaluates only the original operator, never possible future edits.
- Reflection dependencies record the code-object fields that tests and Python's `inspect` read. The selector compiles the old/new source in memory and retains readers of changed fields, even if they never call the function. Reading a function's argument count alone does not make its body constants a dependency.
- Class setup is attributed to all tests in that class. Process-pool work is measured in its originating test context, retaining pickle round trips so serialization dependencies are included.
- The selector applies each patch in memory, compares Python ASTs, and uses complete statement/condition spans to account for short-circuit changes. AST-equivalent edits retain source-reading tests; fixture edits use observed file dependencies.
- Import-time execution, syntax errors, unsupported changes and incomplete source information receive conservative handling. Declaration edits are checked in an isolated import process with all modified modules present together.
- Scheduling evaluates 1–8 workers using integer milliseconds, longest-test-first balancing, and the 2-second cost of every nonempty worker.

The runtime never reads sample answers or a patch-to-failure lookup table. Only `selector.py`, its import/comparison helpers, the baseline maps and the pinned upstream tree are copied into the image.

The image retains SQLGlot's MIT license at `/opt/sqlglot/LICENSE`; a copy is also in [docs/SQLGLOT_LICENSE.txt](docs/SQLGLOT_LICENSE.txt).

## Run

```sh
docker build --platform linux/amd64 -t blast-radius-entry .
python tools/evaluate.py --image blast-radius-entry --label local
```

The evaluation helper works from PowerShell as well as Linux. It mounts only `sample/commits` and `sample/tests.json` into the container. It runs three times with the official CPU, memory, network, filesystem, user and process restrictions, and saves outputs/reports under `evaluation/`.

The original `make sample` command also works in an environment with make, Docker and Python 3.9+. [SCORING.md](SCORING.md) is the official scoring specification; [docs/STARTER_README.md](docs/STARTER_README.md) preserves the original starter instructions.

## Rebuild maps

Use an unmodified checkout of the pinned commit, with its `.py` files and fixtures present. The mapping environment installs the exact official `requirements.lock` plus coverage.py 7.17.4 on the official Python image.

```sh
docker build -f tools/Dockerfile.maps -t rwx-maps .
docker run --rm --network none --cpus 4 --memory 8g \
  -v /absolute/sqlglot:/src:ro -v "$PWD/tools:/tools:ro" \
  -v /absolute/raw-maps:/maps rwx-maps python /tools/build_maps.py --attributes
docker run --rm --network none \
  -v /absolute/sqlglot:/src:ro -v "$PWD:/entry" \
  -v /absolute/raw-maps:/maps:ro rwx-maps python /entry/tools/export_maps.py
```

`--attributes` records class-configuration reads. Generated maps and source hashes are in `artifacts/`; large raw coverage databases, evaluation outputs and mutation results stay outside the submission history. Provenance preserves original instrumentation failures and completed recovery traces; incomplete observations are rejected. The class-fixture supplement is explicitly a four-sentinel trace, not a full-suite run.

```sh
python -m unittest discover -s tests -v
```

The selector tests cover patch reconstruction, source mismatch handling, worker costs, short-circuit conditions, source-sensitive comments, multi-file changes, new tests, import-time failures and collection-time helper effects.

The type trace can be regenerated in the same mapping environment with `python /tools/build_type_profiles.py --source /src --output /profiles`. It must complete the full test suite; the selector verifies the pinned commit and exact collected test identities before using it. Its provenance is in `artifacts/type-profiles-provenance.json`.

The reflection trace uses `python /tools/build_reflection_fields.py --source /src --output /reflections`. It instruments field reads before importing `inspect` and disables pytest assertion rewriting so test modules use the observation loader. The unchanged full suite must pass. Its provenance is in `artifacts/reflection-fields-provenance.json`.

For v5, also collect `build_maps.py --attributes --cold-caches`, union it with the warm map using `export_maps.py --union` and retain the old attribute-key set with `--attribute-keys`. `build_import_profile.py` and `merge_import_profile.py` add eager-import observations. The class-dictionary, collection-call and lambda-call profiles have separate builders in `tools/`; `merge_lambda_profiles.py` requires warm, cold and eager-import modes. Provenance records all inputs and any isolated recovery after instrumentation failures; those recoveries are not described as clean full-suite runs.

## Offline generalization checks

`tools/generate_probes.py` produces seeded source mutations without consulting sample patches or failure labels. `tools/validate_probes.py` applies each mutation to a separate writable copy and executes every test the selector omitted, stopping at the first counterexample. It records code/map hashes and keeps all results under the ignored `evaluation/` directory. A passing probe establishes no omitted failures for that mutation, not universal soundness.

`tools/generate_identity_probes.py` additionally creates three multi-file stress patches from the untouched baseline facts. They change 5 enum, 259 None and 49 object comparisons; all 1620 tests passed for each patch. These are three bundled probes, not 313 individually validated mutations.

`tools/generate_structural_probes.py` generates insertion, deletion, signature and decorator changes from baseline source/coverage. All 12 probes passed their omitted-test checks (18,909 test executions). A separately constructed bytecode-reflection probe exposed a v3.1 omission; v4 retains that failing test, and all its 1443 omitted tests pass.

Official problem: [RWX · Blast Radius](https://www.joinbuilders.org/buildathon/tracks/blast-radius).
