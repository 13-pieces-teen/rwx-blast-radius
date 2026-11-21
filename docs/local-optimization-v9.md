# V9: prefix observation experiment

This practice candidate packages the previously frozen prefix policy. The v8 base selector and scheduler are unchanged. A baseline-only map records exact comparison operands by test, class fixture and collection context. Omissions are restricted to the original canonical prefix before any potential divergence, retaining source-reading, reflection, structural and unknown dependencies.

`tools/context_selector.py` differs from the validated experimental module only in its repository-root path. `semantic_selector.py` is the submission entrypoint. The image uses the existing public pinned Python base and downloads only the pinned upstream source; it contains no experiment patches, mutation failure labels or precomputed patch selections.

## Local evidence before submission

The frozen map covers 1,225 historically observed sites, including 386 previously opaque sites. Warm and cold baseline captures each complete 1,620 passing tests, one skip and 25,440 passing subtests; eager-import collection is separately retained. The compressed map is 2,827,912 bytes, SHA-256 `7847855fd5bfe212d33abc5b95858ec88754e9c2acab9c85dc6ff63f04ff6830`.

Six cohorts were frozen before mutation outcomes, including 50 new synthetic edits in two separately reported groups. All new omissions were validated against complete uninstrumented suites: nine distinct mutated trees plus one explicitly equivalent reused result. Every newly omitted test passed, and all actual failing parent tests were retained. Unchanged selections inherit unknown common baseline penalties; these are not described as independently proven zero-miss cases.

Three interleaved constrained runs per policy and cohort (72 runs total) found prefix differences versus v8 in simulated test time plus analysis CPU: new coverage-conditioned 20 edits -18.354 s; prior held-out 15 edits -14.309 s; prior targeted six -16.597 s; new unconditional 30 edits +2.084 s; public 30 +2.045 s. The 156-case historical diagnostic batch has unchanged simulated test time and noisy CPU differences. These are local heterogeneous, partly overlapping cohorts, not official practice or final scores.

Before publication, all 124 existing Linux regression checks passed. The release image was invoked through its actual Docker CMD under the official runtime restrictions on all six complete cohorts (257 records); every worker assignment matched the previously frozen prefix output exactly. The public sample had zero missed commits and 117.861 simulated seconds. This is a packaging regression check, not a new timing benchmark.

## Limits

The prefix policy depends on the canonical execution order and representative complete baseline observations. Real synthetic shared-state/order counterexamples remain; the contest does not explicitly promise the failure-label execution order. Capture used a CRLF checkout and the existing inline executor instrumentation, while final mutation validation used complete LF Git-object source and native process pools. This validates the specific frozen omissions empirically, not all future inputs or environments. The candidate is submitted to measure official practice behavior, without a claimed 160-second result.

V8 commit `66f69cfd271296ba6055731b1b79fe5b6a649914` and its 214.3-second official result remain available in repository history. An official v9 result will be recorded separately after scoring.
