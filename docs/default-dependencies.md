# Default parameter dependencies

`default_dependencies.DefaultsMixin` refines a single literal boolean default
change in an ordinary function. Restoring that one literal must restore the
whole AST. Decorators, nested definitions, generators, asynchronous functions,
unknown sources, insertions and other semantic edits retain the base selector.
Existing source-read, reflection, structural, import and unknown-test safeguards
remain active. Missing, truncated, malformed or mismatched profiles fall back.

The profile comes only from untouched SQLGlot commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. It observes 369 functions and 560
boolean parameters, and binds all 250 mapped source/test files and 1621 test
identities. Warm and cold modes each passed the full 1621-test lifecycle,
including 25440 passing subtests. Eager mode imported SQLGlot modules and
collected all test identities. A separate three-mode identity audit also passed.
Every source/fixture snapshot remained unchanged; all six containers exited
successfully without OOM and were removed.

Call entry records exact bound values before the function body runs. A value
equal to the original default remains possibly implicit. Only an opposite
boolean proves that call did not use the original default. Collection and class
fixtures use the same distinction. Reads of `__defaults__`, `__kwdefaults__`,
signatures and aliases are independent dependencies. CPython audit events also
cover direct `object.__getattribute__`, `operator.attrgetter`, default/code
replacement and same-code function cloning. The observer suppresses only its
own metadata inspections.

The strict source/test effect analysis marks one test as a persistent writer
and the other 1620 as opaque. This is deliberately visible: it does not pretend
dynamic calls and aliases have been proved harmless. With this artifact,
refinement therefore requires **no affected context**: no possible-default or
opaque call, no metadata dependency, no unobserved body coverage, and no
source/reflection consumer that could propagate a change. That supports unused
functions and functions whose calls always supply the opposite boolean. It
does not enable the earlier per-test body-only rule or its shared-state misses.

Regression checks cover metadata/alias/fixture caching, persistent globals,
returned-value escapes, latent write branches, local containers holding shared
aliases, rebinding, descriptors, inherited helpers, changed default tables and
cloned functions. They also distinguish explicit opposite values in collection
from actual default use, preserve observations absent from old body coverage,
and reject incomplete optional profile fields.

The profile's SHA-256 is
`4fa72cf8067b79f4c8887cc15d5ce772e2dc39ae92d3f44508f0160e644bfdda`.
See `artifacts/default-dependencies-provenance.json` for run and source hashes,
and `tools/defaults_profile/README.md` for exact published collector snapshots.
No mutation patches, failure labels, expected selections or costs enter the
profile. Parameter and metadata observations use serialized `InlineExecutor`
work for attribution; native pool behavior is verified separately.

This is a fixed-suite observation model. It is not a theorem about arbitrary
Python heap introspection, unseen tests or all possible nondeterministic paths.
The known metadata/state counterexamples are regression requirements, not a
claim that every conceivable Python reflection mechanism has been covered.
