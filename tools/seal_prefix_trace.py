"""Validate and bind immutable full-prefix outputs, including legacy captures."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

from export_maps import validate_coverage, validate_observations
from recover_map_trace import validate_prefix_sources
from retrace_prefix import validate_prefix


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path('/src'))
    args = parser.parse_args()
    base_bytes = (args.base/'observations.json').read_bytes()
    base = json.loads(base_bytes)
    prefix = json.loads((args.prefix/'prefix-provenance.json').read_text())
    observed = json.loads((args.prefix/'observations.json').read_text())
    validate_prefix(base, prefix)
    if prefix.get('complete') is not True or prefix['base_observations_sha256'] != hashlib.sha256(base_bytes).hexdigest():
        raise ValueError('prefix is incomplete or belongs to another original trace')
    if (observed['items'] != [prefix['target']] or
            observed['results'] != [row for row in prefix['prefix_results'] if row['id'] == prefix['target']] or
            observed['generator_hashes'] != prefix['generator_hashes']):
        raise ValueError('prefix observations do not match recorded target execution')
    manifest = json.loads(gzip.decompress((Path(__file__).resolve().parents[1]/'artifacts/baseline-map.json.gz').read_bytes()))
    source_verification = validate_prefix_sources(prefix, manifest, args.source)
    validate_observations(observed)
    validate_coverage(args.prefix, observed)
    hashes = {name: hashlib.sha256((args.prefix/name).read_bytes()).hexdigest()
              for name in ('observations.json', '.coverage', 'prefix-provenance.json')}
    if prefix.get('trace_sha256') and any(prefix['trace_sha256'].get(name) != hashes[name]
                                         for name in ('observations.json', '.coverage')):
        raise ValueError('captured trace hash mismatch')
    result = {'format_version': 1, 'complete': True, 'sha256': hashes,
              'base_observations_sha256': prefix['base_observations_sha256'],
              'target': prefix['target'], 'executed_prefix_tests': len(prefix['executed_prefix']),
              'source_verification': source_verification,
              'seal_timing': 'after capture; validated exact target reports and observer hashes, then bound immutable outputs',
              'capture_embedded_hashes': bool(prefix.get('trace_sha256')),
              'original_generator_identity': ('recorded' if base.get('generator_hashes') else 'unverified_missing_metadata'),
              'known_observer_changes': ['Class fixture wrapping moved before pytest captures bound methods'],
              'sealer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    destination = args.prefix/'prefix-seal.json'
    if destination.exists():
        raise ValueError('prefix seal already exists; preserve the prior evidence')
    destination.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
