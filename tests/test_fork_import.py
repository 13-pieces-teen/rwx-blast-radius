import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from tools import fork_import

ROOT = Path(__file__).resolve().parents[1]
DRIVER = '''
import json, os, signal, subprocess, sys, tempfile
from pathlib import Path
from tools import fork_import
assert fork_import._can_fork()
temp = tempfile.TemporaryDirectory()
root = Path(temp.name)
(root / "sqlglot").mkdir()
(root / "sqlglot/__init__.py").write_text("BASELINE = True\\n")
def check(code, timeout=2):
    request = json.dumps({"source": str(root), "overrides": {"sqlglot/__init__.py": code},
                          "imports": ["sqlglot/__init__.py"]}).encode()
    return fork_import.run([sys.executable, "-B", str(Path.cwd()/"tools/import_check.py")],
                           input=request, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout)
'''


class ForkImportTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "fork"), "fork is Linux runtime optimization")
    def test_fresh_children_isolate_variant_state(self):
        result = subprocess.run([sys.executable, "-c", DRIVER + '''
assert check("import builtins, sys\\nbuiltins.variant_secret=42\\nsys.modules['variant_secret']=object()\\n").returncode == 0
assert check("import builtins, sys\\nassert not hasattr(builtins, 'variant_secret')\\nassert 'variant_secret' not in sys.modules\\n").returncode == 0
assert "sqlglot" not in sys.modules
assert "variant_secret" not in sys.modules
assert check("import sys\\nassert sys.dont_write_bytecode\\nassert sys.argv[0].endswith('tools/import_check.py')\\n").returncode == 0
print("isolated")
'''], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "isolated")

    @unittest.skipUnless(hasattr(os, "fork"), "fork is Linux runtime optimization")
    def test_failures_timeouts_and_signals_are_reaped(self):
        result = subprocess.run([sys.executable, "-c", DRIVER + '''
assert check("VALUE = 1\\n").returncode == 0
assert check("raise RuntimeError('expected')\\n").returncode != 0
assert check("if:\\n").returncode != 0
assert check("import os,signal\\nos.kill(os.getpid(),signal.SIGTERM)\\n").returncode == -signal.SIGTERM
assert check("import sys\\nsys.stdout.write('x'*1000000)\\n").returncode == 0
try:
    check("while True: pass\\n", timeout=.05)
    raise AssertionError("did not time out")
except subprocess.TimeoutExpired:
    pass
try:
    os.waitpid(-1, os.WNOHANG)
    raise AssertionError("unreaped child")
except ChildProcessError:
    pass
print("reaped")
'''], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "reaped")

    def test_test_runner_uses_original_exec_isolation(self):
        self.assertFalse(fork_import._can_fork())
        with patch.object(fork_import, "_REAL_RUN", return_value="exec") as fallback:
            result = fork_import.run([sys.executable, "-B", "checker.py"], input=b"{}",
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8)
        self.assertEqual(result, "exec")
        fallback.assert_called_once()

    @unittest.skipUnless(hasattr(os, "fork"), "fork is Linux runtime optimization")
    def test_contaminated_or_threaded_parent_uses_exec(self):
        result = subprocess.run([sys.executable, "-c", DRIVER + '''
import threading
original = fork_import._REAL_RUN
fallbacks = []
def observed(*args, **kwargs):
    fallbacks.append(True)
    return original(*args, **kwargs)
fork_import._REAL_RUN = observed
sys.modules['sqlglot'] = object()
assert not fork_import._can_fork()
assert check("VALUE = 1\\n").returncode == 0
del sys.modules['sqlglot']
gate = threading.Event()
worker = threading.Thread(target=gate.wait)
worker.start()
try:
    assert not fork_import._can_fork()
    assert check("VALUE = 1\\n").returncode == 0
finally:
    gate.set()
    worker.join()
assert len(fallbacks) == 2
assert fork_import._can_fork()
print("fallback")
'''], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "fallback")


if __name__ == "__main__":
    unittest.main()
