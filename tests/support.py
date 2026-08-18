"""Shared test scaffolding: the module under test (loaded once per test run) and the
monkeypatch base class every suite uses.

`unittest discover -s tests` puts this directory on sys.path, so test modules import
this as a plain `import support` / `from support import cu`.
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location(
    "claude_usage", os.path.join(_HERE, "..", "claude-usage.py"))
cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cu)


def raiser(exc):
    """A stub that raises `exc` whatever it is called with."""
    def _raise(*a, **k):
        raise exc
    return _raise


class Patched(unittest.TestCase):
    """Base class with attribute monkeypatching on the module under test."""

    def _patch(self, name, stub):
        orig = getattr(cu, name)
        setattr(cu, name, stub)
        self.addCleanup(setattr, cu, name, orig)


def run_capturing(func, *args, **kwargs):
    """Call func with stdout/stderr captured and any SystemExit intercepted.

    Returns (code, stdout, stderr); code is 0 when func returned without exiting.
    """
    import contextlib, io
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            func(*args, **kwargs)
        except SystemExit as ex:
            code = ex.code
    return code, out.getvalue(), err.getvalue()
