"""Installed identity proofs at inert native supervisor executable boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.linux_only
@pytest.mark.parametrize("case,reason", [
    ("custom", "deadline"), ("named", "deadline"), ("alias", "deadline"),
    ("default", "deadline"),
    ("wrong_home", "profile_mismatch"), ("wrong_user", "service_account_mismatch"),
    ("missing_identity", "service_identity_unverified"),
    ("env_file", "service_identity_unverified"), ("dynamic_user", "service_identity_unverified"),
    ("wrong_command", "service_identity_unverified"), ("command_profile", "profile_mismatch"),
    ("dropin_home", "profile_mismatch"), ("manager_home", "profile_mismatch"),
])
def test_ensure_checks_effective_service_binding_before_start(tmp_path, monkeypatch, case, reason):
    from hermes_cli import gateway_runtime as runtime
    from hermes_cli.gateway_runtime_service import service_suffix
    import pwd

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / (".hermes" if case == "default" else "custom root")
    if case == "named":
        home = home / "profiles" / "worker"
    home.mkdir(mode=0o700, parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    suffix = service_suffix(home)
    unit = tmp_path / ".config/systemd/user" / f"hermes-gateway{'-' + suffix if suffix else ''}.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f'[Service]\nExecStart={sys.executable} -m hermes_cli.main gateway run\nEnvironment="HERMES_HOME={home}"\n', encoding="utf-8")
    original = unit.read_bytes()
    configured = home
    if case in {"wrong_home", "dropin_home"}:
        configured = tmp_path / "unrequested"
    if case == "alias":
        configured = tmp_path / "alias"
        configured.symlink_to(home, target_is_directory=True)
    command = f'{sys.executable} -m hermes_cli.main gateway run'
    if case == "named":
        command += " --profile worker"
    if case == "command_profile":
        command += " --profile stranger"
    if case == "wrong_command":
        command = '/bin/echo hermes_cli.main gateway run'
    props = dict(LoadState="loaded", ActiveState="inactive", SubState="dead", UnitFileState="enabled",
                 User=str(os.getuid()), DynamicUser="no", Environment=f'"HERMES_HOME={configured}" "HERMES_SUPERVISED_CHILD=1"',
                 EnvironmentFiles="", PassEnvironment="", UnsetEnvironment="", PAMName="",
                 RootDirectory="", RootImage="", ExecStartPre="", ExecCondition="",
                 ExecStart=f'{{ path={command.split()[0]} ; argv[]={command} ; ignore_errors=no ; }}',
                 DropInPaths=str(unit.parent / (unit.name + '.d') / 'override.conf') if case == "dropin_home" else "")
    manager_env = ""
    if case == "wrong_user":
        props['User'] = str(os.getuid() + 1)
    if case == "missing_identity":
        props = {k: props[k] for k in ("LoadState", "ActiveState", "SubState", "UnitFileState")}
    if case == "env_file":
        env_file = tmp_path / "override.env"
        env_file.write_text(f'HERMES_HOME={tmp_path / "other"}\n', encoding="utf-8")
        props['EnvironmentFiles'] = f'{env_file} (ignore_errors=no)'
    if case == "dynamic_user":
        props['DynamicUser'] = 'yes'
    if case == "manager_home":
        props['Environment'] = 'HERMES_SUPERVISED_CHILD=1'
        manager_env = f'HERMES_HOME={tmp_path / "other"}\n'
    if case == "default":
        # An older default install may rely on the user manager's HOME.
        props['Environment'] = 'HERMES_SUPERVISED_CHILD=1'
        manager_env = f'HOME={tmp_path}\n'
    if case == "system":
        props['User'] = pwd.getpwuid(os.getuid()).pw_name
    definition = tmp_path / "effective.json"
    definition.write_text(json.dumps({'props': props, 'system': case == 'system', 'env': manager_env}), encoding="utf-8")
    calls = tmp_path / 'calls.jsonl'
    executable = tmp_path / 'inert-supervisor'
    executable.write_text('#!' + sys.executable + '\nimport json,sys\nfrom pathlib import Path\n'
        + f'p=Path({str(tmp_path)!r})\nd=json.loads((p/"effective.json").read_text())\n'
        + 'with (p/"calls.jsonl").open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
        + 'if "show-environment" in sys.argv: print(d["env"])\n'
        + 'elif "show" in sys.argv:\n'
        + ' if ("--user" not in sys.argv) == d["system"]: print("\\n".join(k+"="+v for k,v in d["props"].items()))\n'
        + ' else: print("LoadState=not-found")\n'
        + 'elif "start" not in sys.argv: sys.exit(91)\n', encoding='utf-8')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    from hermes_cli import gateway as gw
    monkeypatch.setattr(gw, "_systemctl_cmd", lambda system=False: [str(executable)] + ([] if system else ["--user"]))
    result = runtime.ensure_gateway_runtime(home, timeout=0.5)
    assert result.reason_code == reason
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    starts = [cmd for cmd in commands if 'start' in cmd]
    assert len(starts) == (1 if reason == 'deadline' else 0)
    assert all('--no-ask-password' in cmd for cmd in starts)
    if case != 'system':
        assert unit.read_bytes() == original
    assert not (home / 'logs').exists()
