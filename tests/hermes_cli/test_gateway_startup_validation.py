"""Invalid launch options must not inspect configuration they bypass."""
from argparse import Namespace


def test_invalid_options_are_reported_before_reading_profile(monkeypatch, capsys):
    from hermes_cli.gateway_chat_startup import launch_gateway_chat

    def unexpected():
        raise AssertionError("Unsupported launch read profile configuration")

    monkeypatch.setattr("hermes_cli.main._has_any_provider_configured", unexpected)
    assert launch_gateway_chat(Namespace(safe_mode=True)) == 2
    assert "--safe-mode" in capsys.readouterr().err
