"""Fresh exec probes for the private managed safe-worker bootstrap contract."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def run_worker(tmp_path, body, mode="safe"):
    home = tmp_path / mode
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text("agent:\n  max_turns: 17\n", encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=str(home), HOME=str(home), PROBE_MODE=mode)
    for key in ("HERMES_SAFE_MODE", "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)], env=env,
        cwd=Path(__file__).resolve().parents[2], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("mode", ["safe", "config", "ordinary"])
def test_worker_config_is_frozen_before_any_profile_read(tmp_path, mode):
    result = run_worker(tmp_path, '''
        import json, os, sys, threading
        from pathlib import Path
        mode = os.environ["PROBE_MODE"]
        if mode != "ordinary":
            from agent.safe_worker_policy import _bind_safe_worker_policy
            snapshot = {"agent": {"max_turns": 3}, "terminal": {"cwd": "/explicit"}}
            _bind_safe_worker_policy(safe_mode=mode == "safe", ignore_user_config=True, config=snapshot)
            snapshot["agent"]["max_turns"] = 999
        reads = []
        def audit(event, args):
            if event == "open" and str(args[0]).endswith("config.yaml") and args[1] != "w":
                reads.append(str(args[0]))
        sys.addaudithook(audit)
        from hermes_cli.config import load_config, load_config_readonly, read_raw_config, read_raw_config_readonly, DEFAULT_CONFIG
        from hermes_cli import env_loader
        callbacks = []
        env_loader._apply_managed_env = lambda: callbacks.append("managed")
        env_loader._apply_external_secret_sources = lambda home: callbacks.append("secrets")
        env_loader._reapply_terminal_config_bridge = lambda home: callbacks.append("terminal")
        env_loader.load_hermes_dotenv()
        cfg = load_config()
        assert cfg["compression"] == DEFAULT_CONFIG["compression"]
        first = cfg["agent"]["max_turns"]
        cfg["agent"]["max_turns"] = 123
        if mode != "ordinary":
            Path(os.environ["HERMES_HOME"], "config.yaml").write_text("[broken YAML")
            # Audit excludes this fixture write, not the preceding reads.
        results = []
        def check():
            for loader in (load_config, load_config_readonly, read_raw_config, read_raw_config_readonly):
                results.append(loader()["agent"]["max_turns"])
        t = threading.Thread(target=check); t.start(); t.join()
        print(json.dumps({"first": first, "values": results, "reads": reads, "callbacks": callbacks}))
    ''', mode)
    if mode == "ordinary":
        assert result["first"] == 17 and result["reads"]
        assert result["callbacks"] == ["secrets", "managed", "terminal"]
    else:
        assert result == {"first": 3, "values": [3, 3, 3, 3], "reads": [], "callbacks": []}


def test_private_worker_binding_is_one_shot_typed_and_not_env_selected(tmp_path):
    result = run_worker(tmp_path, '''
        import json, os
        from dataclasses import FrozenInstanceError
        from agent.safe_worker_policy import _bind_safe_worker_policy, safe_worker_enabled, worker_config_snapshot
        os.environ["HERMES_SAFE_MODE"] = "1"
        assert not safe_worker_enabled()
        for value in (1, "true", None):
            try:
                _bind_safe_worker_policy(safe_mode=value, ignore_user_config=True, config={})
            except (TypeError, ValueError):
                pass
            else:
                raise AssertionError("non-boolean accepted")
        policy = _bind_safe_worker_policy(safe_mode=True, ignore_user_config=False, config={})
        assert policy.ignore_user_config and safe_worker_enabled()
        try:
            policy.safe_mode = False
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError("mutable policy")
        try:
            _bind_safe_worker_policy(safe_mode=False, ignore_user_config=True, config={})
        except RuntimeError:
            pass
        else:
            raise AssertionError("rebound policy")
        assert worker_config_snapshot() == {}
        print(json.dumps({"bound": safe_worker_enabled()}))
    ''')
    assert result == {"bound": True}
