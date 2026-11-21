# Reproduce original-order prefix recovery

An isolated retry can import modules that were already loaded at the failed
test's original position. Those import-declaration edges must not be confused
with dependencies observed in the original suite state. The commands below
rebuild from the immutable original trace and a successful same-order prefix.
They do not remove any executable, symbol, attribute or module edge from the
original trace. Earlier isolated retraces remain separate historical evidence.

Use the existing mapping image, the unchanged pinned source at `/src`, the
repository at `/entry`, and a writable raw-data directory at `/raw`. Use fresh
output directories for each invocation. These examples use the two actual
instrumentation failures; the tools accept any recorded failed `--target`.

```sh
# Complete original collection and prefix execution, trace collection + target.
python /entry/tools/retrace_prefix.py --source /src \
  --base /raw/maps-v7-cold --output /raw/maps-v7-prefix-retry \
  --target tests/test_generator.py::TestGenerator::test_generator_memory \
  --cold-caches --timeout 900

# Explicit post-capture validation and immutable file-hash binding.
python /entry/tools/seal_prefix_trace.py --source /src \
  --base /raw/maps-v7-cold --prefix /raw/maps-v7-prefix-retry

python /entry/tools/recover_map_trace.py --source /src \
  --base /raw/maps-v7-cold --supplement /raw/maps-v7-prefix-retry \
  --output /raw/maps-v7-prefix-recovered \
  --reason "Original cold trace plus successful full-prefix target retrace; isolated-only additions excluded"

python /entry/tools/retrace_prefix.py --source /src \
  --base /raw/maps-v6 --output /raw/maps-v6-prefix-retry \
  --target tests/test_optimizer.py::TestOptimizer::test_tpcds --timeout 900

python /entry/tools/seal_prefix_trace.py --source /src \
  --base /raw/maps-v6 --prefix /raw/maps-v6-prefix-retry

python /entry/tools/recover_map_trace.py --source /src \
  --base /raw/maps-v6 --supplement /raw/maps-v6-prefix-retry \
  --output /raw/maps-v6-prefix-recovered \
  --reason "Original warm trace plus successful full-prefix target retrace; isolated-only additions excluded"
```

The sealer requires every preceding test to complete successfully, the original
test order and cache mode, all three target phases, matching target reports and
observer hashes, and valid coverage. It checks raw source-byte hashes against
the captured prefix and CRLF-normalized hashes against the pinned map manifest.
It writes `prefix-seal.json` separately without altering the observation,
coverage or prefix-provenance files. The recorded initial prefix captures have
`capture_embedded_hashes: false`: this is an explicit binding after capture.
Legacy original traces have no generator hashes; provenance states that their
observer identity is unverified, and records the known class-fixture hook fix.

To rebuild a candidate, export warm first, then cold union warm, preserving a
trusted historical baseline map and its original attribute-key catalog. Here
`/previous` is an immutable copy of the earlier published artifact directory
(for the v7 audit, v4 commit `5887d18` supplied this historical union).

```sh
python /entry/tools/export_maps.py --source /src --raw /raw/maps-v6-prefix-recovered \
  --output /candidate-warm/baseline-map.json.gz \
  --union /previous/baseline-map.json.gz --attribute-keys /previous/baseline-map.json.gz

python /entry/tools/export_maps.py --source /src --raw /raw/maps-v7-prefix-recovered \
  --output /candidate-stage/baseline-map.json.gz \
  --union /candidate-warm/baseline-map.json.gz --attribute-keys /previous/baseline-map.json.gz
```

Copy the existing validated supplemental profiles and their provenance files to
`/candidate-stage`, keeping its newly exported baseline map/provenance. Then
apply the normal independent supplements; all outputs must be separate paths.

```sh
python /entry/tools/merge_import_profile.py --input /candidate-stage \
  --profile /entry/artifacts/import-profile.json.gz --output /candidate-import
python /entry/tools/merge_class_setup_profile.py --input /candidate-import \
  --profile /entry/artifacts/class-setup-profile.json.gz --output /candidate-final
```

Verify that original warm/cold and historical-baseline edges are subsets of the
candidate, and that removals relative to the prior recovered map occur only in
the old isolated-only supplements. Compare selections and execute any newly
omitted tests before promotion. A passing prefix is not a complete-suite pass.
