# v10: default dependencies and shared-executor propagation

Official submission `BB-8DE3F22C` scored **214.6 seconds**, with zero missed commits in all three runs. Simulated time is unchanged from v8 at 211.977 seconds; the extra selector CPU makes the displayed total 0.3 seconds slower. V8 remains automatically entered in the final. [Official result](official-practice-v10-2026-11-22.json).

The selected candidate combines a narrow default-argument optimization with a generic shared-executor dependency repair. Production code was frozen before the six-cohort run on 2026-11-22. No patch-to-failure answers enter the image; all shipped profiles come from the pinned unmodified SQLGlot source.

The candidate image is `sha256:a27275842326f66dee52d651989a2bca55acd17a2b35e1ffcb3c61ce999cd4f8`. All 806 input patches produced valid output under official runtime restrictions. Existing audited evidence includes 261 complete native results and 30 supplied public labels: the candidate misses none of these and introduces no new observed partial-result misses. The shared-pool repair closes one known v8 missed commit in the 160-case holdout. Remaining cases without complete truth are not counted as passing tests.

| Cohort | Patches | v8 simulated seconds | v10 simulated seconds | v10 container CPU seconds, one run |
| --- | ---: | ---: | ---: | ---: |
| Development | 400 | 3951.539 | 3969.441 | 58.158993 |
| Holdout | 160 | 1765.258 | 1775.241 | 26.427628 |
| Targeted | 40 | 762.931 | 689.078 | 9.977014 |
| Public | 30 | 117.861 | 117.861 | 1.291067 |
| Historical | 156 | 1839.326 | 1819.682 | 21.396147 |
| Reserve | 20 | 177.199 | 182.013 | 3.162385 |

Simulated times exclude miss penalties and selector CPU. The known v8 holdout miss adds a further 300 seconds to its score; v10 retains those failures. These cohorts have different sampling schemes and cannot be combined into an estimate of the official hidden score. CPU figures are single container observations, not three-run official medians.

The default rule is intentionally narrow: nonempty affected contexts with unresolved persistent effects still use v8 selection. In particular, this release does not claim the broader `and_.copy` optimization. The pool guard increases selections where initialization or deserialization can poison a class-shared executor. It does not prove coverage of every possible new native-process failure mechanism.

The final Linux regression suite passes 164 test methods, including 30 actual-execution subcases in the additional default-profile test. Detailed baseline-profile provenance and reproduction instructions are in [default dependencies](default-dependencies.md) and [shared-executor dependencies](shared-executor-dependencies.md). Local run records remain in ignored `evaluation/v10/runs/candidate-first`; only baseline profiles and runtime implementation are shipped. Official submission results are recorded separately after scoring.
