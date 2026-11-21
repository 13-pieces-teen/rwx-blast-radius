"""Submission entrypoint for the frozen prefix observation policy."""
import argparse
import json
from pathlib import Path
import sys

from selector import schedule
from tools.context_selector import PrefixExperimentalSelector


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--source', type=Path, default=Path('/opt/sqlglot'))
    parser.add_argument('--maps', type=Path, default=root/'artifacts/baseline-map.json.gz')
    parser.add_argument('--profile', type=Path, default=root/'artifacts/ordered-comparison-contexts.json.gz')
    args = parser.parse_args()
    tests = json.loads((args.data/'tests.json').read_text())['tests']
    selector = PrefixExperimentalSelector(args.source, args.maps, tests, args.profile)
    for path in sorted((args.data/'commits').glob('*.patch')):
        selected = selector.select(path.read_text(encoding='utf-8'))
        workers = schedule(selected)
        print(json.dumps({'commit': path.stem, 'workers': workers}, separators=(',', ':')), flush=True)
        print(f'{path.stem}: {len(selected)} tests, {len(workers)} workers', file=sys.stderr)


if __name__ == '__main__':
    main()
