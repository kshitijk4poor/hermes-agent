"""Cron ``.py`` scripts run on PM's committed environment interpreter (#123440).

Under the PM runtime ``sys.executable`` is the bare store Python and the subprocess
sanitizer strips PYTHONPATH, so a script spawned on it cannot import Hermes modules
or managed dependencies. The committed environment's own interpreter carries its
site-packages via ``pyvenv.cfg``.
"""
import sys
from pathlib import Path

import pytest


@pytest.mark.platforms("posix")
def test_py_script_runs_on_committed_environment_interpreter(tmp_path, monkeypatch):
    import pm.environments as pe
    from cron.scheduler_script import _script_argv

    script = tmp_path / "probe.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    venv = tmp_path / "gen1" / "venv"
    python = pe.venv_python(venv, windows=False)
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    repo = Path(pe.__file__).resolve().parents[1]

    monkeypatch.setattr(pe, "committed_venv", lambda root: venv)
    assert _script_argv(script) == ([str(python), str(script)], {"PYTHONPATH": str(repo)}, None)

    # No committed generation (source checkout, pre-PM venv): unchanged launch.
    monkeypatch.setattr(pe, "committed_venv", lambda root: None)
    assert _script_argv(script) == ([sys.executable, str(script)], {}, None)
