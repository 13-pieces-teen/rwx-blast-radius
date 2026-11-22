# Default dependency collector snapshots

`binding_v3` and `identity_v1` preserve the exact collector and observer bytes
executed for the released profile. They are separate because the second run
adds function code/default-table assignment auditing without repeating expensive
call-entry profiling. The two observer versions are deliberately not collapsed
into a rewritten collector and described as the original experiment.

The input is the complete, untouched SQLGlot commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, including tests and fixtures, plus
`artifacts/baseline-map.json.gz`. No patch corpus, expected selection, mutation
failure, cost or leaderboard label is an input. Python 3.13.18 and pytest 9.2.0
were used. The full warm and cold suites must pass; eager mode imports every
SQLGlot module and collects the full suite. Each mode uses a fresh process and
source copy. `InlineExecutor` retains initializer/call/result pickle boundaries
for context attribution; it is not a native process-pool correctness run.

Inside the baseline environment, use `PYTHONPATH` to include the repository's
`tools` directory and the selected snapshot directory, then run:

```text
python -B tools/defaults_profile/binding_v3/collect_profile.py --source SOURCE --map artifacts/baseline-map.json.gz --output OUTPUT/warm --mode warm
python -B tools/defaults_profile/identity_v1/collect_profile.py --identity-only --source SOURCE --map artifacts/baseline-map.json.gz --output IDENTITY/warm --mode warm
```

Repeat with `cold` and `eager-import-collection`. Each invocation needs a fresh
`/tmp/default-profile-source`; the official runs used separate containers on
CPUs 2-3, with two CPUs and 3 GiB memory each. Retain raw observations, lifecycle
records, input snapshots, immutable image inspection and cleanup results. The
merge entry validates that run evidence, not just a claimed `complete` flag.

Generate the conservative state guard with `build_state_guard.py --source
SOURCE --map MAP --output GUARD.json`. Merge with `merge_profile.py --run OUTPUT
--identity-run IDENTITY --guards GUARD.json --output PROFILE.json.gz`. The merge
expects the original host evidence structure described in the profile's
provenance; it does not fabricate missing Docker evidence for manual runs.

Unknown dispatch, external calls, descriptors and mutation aliases remain
opaque. For this pinned suite the strict guard rejects every nonempty affected
test set. Actual refinement therefore uses the narrower global condition:
no default-using or opaque calls, no metadata consumers, and no unobserved body
coverage. Explicit opposite boolean values are distinguished from default use,
including collection and class fixtures. This is a bound-argument and metadata
observation model for the fixed suite, not a theorem about arbitrary Python heap
introspection or unseen test executions.
