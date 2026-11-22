"""Fresh-process import-state observations with no mutant imports in the parent."""
from __future__ import annotations

import contextlib
import gzip
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time


def _capture(request, output):
    from tools.import_state import snapshot
    # Mutated code may print; only our JSON file belongs to the protocol.
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        result = snapshot(request['source'], request['overrides'], request['imports'],
                          normalization_policy=request.get('normalization_policy'))
    Path(output).write_text(json.dumps(result, separators=(',', ':')), encoding='utf-8')


def run_snapshot(request, timeout=8):
    """Return a complete fresh import snapshot, or raise on an import failure."""
    descriptor, output = tempfile.mkstemp(prefix='import-state-', suffix='.json')
    os.close(descriptor)
    try:
        can_fork = (hasattr(os, 'fork') and threading.active_count() == 1
                    and not any(n in ('sqlglot', 'pytest', 'unittest') or n.startswith('sqlglot.')
                                for n in sys.modules))
        if not can_fork:
            result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), output],
                                    input=json.dumps(request).encode(), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=timeout)
            if result.returncode:
                raise RuntimeError('import snapshot failed: ' + result.stderr.decode(errors='replace')[-300:])
        else:
            # Load only the stdlib helper before forking, not SQLGlot.
            from tools import import_state
            started = time.monotonic()
            pid = os.fork()
            if pid == 0:
                try:
                    fd = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(fd, 1)
                    os.dup2(fd, 2)
                    os.close(fd)
                    sys.dont_write_bytecode = True
                    _capture(request, output)
                except BaseException:
                    os._exit(1)
                os._exit(0)
            reaped = False
            try:
                while True:
                    done, status = os.waitpid(pid, os.WNOHANG)
                    if done:
                        reaped = True
                        if os.waitstatus_to_exitcode(status):
                            raise RuntimeError('modified modules failed import snapshot')
                        break
                    if time.monotonic() - started >= timeout:
                        raise subprocess.TimeoutExpired('import-state', timeout)
                    time.sleep(.005)
            finally:
                if not reaped:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(pid, 0)
        return json.loads(Path(output).read_bytes())
    finally:
        Path(output).unlink(missing_ok=True)


class ImportStateProbe:
    def __init__(self, source, path, baseline_map):
        self.source = str(source)
        self.profile = json.loads(gzip.decompress(Path(path).read_bytes()))
        if self.profile.get('source_commit') != baseline_map['source_commit']:
            raise ValueError('import-state source identity mismatch')
        expected = {k: v for k, v in baseline_map['source_hashes'].items() if k.startswith('sqlglot/')}
        if self.profile.get('source_hashes') != expected:
            raise ValueError('import-state source hashes mismatch')
        self.baseline = self.profile['snapshot']
        self.imports = self.profile['imports']

    def compare(self, overrides):
        after = run_snapshot({'source': self.source, 'overrides': overrides, 'imports': self.imports,
                              'normalization_policy': self.baseline.get('normalization_policy')})
        from tools.import_state import diff
        changed = diff(self.baseline, after)['changed']
        return changed, self.baseline, after


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    _capture(json.load(sys.stdin), sys.argv[1])
