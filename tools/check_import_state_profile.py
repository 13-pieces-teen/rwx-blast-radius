"""Check the shipped profile against the actual image during its build."""
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.import_state_probe import ImportStateProbe


def main():
    baseline = json.loads(gzip.decompress((ROOT/'artifacts/baseline-map.json.gz').read_bytes()))
    probe = ImportStateProbe(Path('/opt/sqlglot'), ROOT/'artifacts/import-state.json.gz', baseline)
    changed, _, _ = probe.compare({})
    if changed:
        raise RuntimeError('pristine image differs from import profile: ' +
                           ', '.join(row['key'] for row in changed[:20]))
    print('Import-state profile matches pristine runtime image.')


if __name__ == '__main__':
    main()
