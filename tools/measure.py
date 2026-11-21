"""Local evaluation wrapper, not part of the competition entrypoint."""
import json
from pathlib import Path
import resource
import runpy
import sys
import time

path = sys.argv[1]
sys.argv = sys.argv[1:]
# Match `python /app/selector.py`: run_path alone does not add the script's
# directory, so separately packaged runtime helpers otherwise cannot import.
sys.path.insert(0, str(Path(path).resolve().parent))
started = time.monotonic()
try:
    runpy.run_path(path, run_name="__main__")
finally:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    print("RWX_METRICS " + json.dumps({
        "cpu_seconds": own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
        "wall_seconds": time.monotonic() - started,
        "max_rss_kb": max(own.ru_maxrss, children.ru_maxrss),
    }), file=sys.stderr)

