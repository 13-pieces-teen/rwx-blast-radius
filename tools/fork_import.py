"""Run import checks in fresh children without restarting the Python interpreter.

The parent only loads the stdlib checker, never the SQLGlot package or a variant.
Every request gets a new child; callers use only its success/failure status.
"""
import importlib.util
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

_REAL_RUN = subprocess.run
_CHECKER = None


def _can_fork():
    # SQLGlot checks for pytest/unittest at import time. A test runner must use
    # exec isolation too, so those modules cannot influence the import result.
    return (hasattr(os, "fork") and threading.active_count() == 1
            and not any(name in ("sqlglot", "pytest", "unittest") or name.startswith("sqlglot.")
                        for name in sys.modules))


def run(command, *, input, stdout, stderr, timeout):
    """subprocess.run's narrow interface used by Selector's import validation."""
    global _CHECKER
    if (not _can_fork() or len(command) != 3 or command[1] != "-B"
            or stdout != subprocess.DEVNULL or stderr != subprocess.PIPE):
        return _REAL_RUN(command, input=input, stdout=stdout, stderr=stderr, timeout=timeout)
    checker_path = Path(command[-1])
    if _CHECKER is None or _CHECKER.__file__ != str(checker_path):
        spec = importlib.util.spec_from_file_location("_isolated_import_checker", checker_path)
        _CHECKER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_CHECKER)
    if not _can_fork():
        return _REAL_RUN(command, input=input, stdout=stdout, stderr=stderr, timeout=timeout)
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
            # Suppress mutant output and never flush inherited selector buffers.
            fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.close(fd)
            sys.argv = command[2:]
            sys.dont_write_bytecode = True
            sys.stdin = io.StringIO(input.decode())
            _CHECKER.main()
        except BaseException:
            os._exit(1)
        os._exit(0)
    reaped = False
    try:
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped = True
                return subprocess.CompletedProcess(command, os.waitstatus_to_exitcode(status), stderr=b"")
            if time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(.005)
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
