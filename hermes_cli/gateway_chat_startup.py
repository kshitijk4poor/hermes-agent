"""Frontend startup checks shared by classic chat and top-level one-shot."""
from __future__ import annotations


def launch_gateway_chat(args) -> int:
    try:
        from hermes_cli.main import (
            _confirm_startup_expensive_model_override,
            _first_run_setup_guard,
            _has_any_provider_configured,
        )
        from hermes_cli.gateway_chat import launch_from_args
        import os

        # Consent belongs before discovery/admission, even with --yolo.
        _confirm_startup_expensive_model_override(args)
        # A resumed/remote session owns its provider policy. Explicit endpoint/key
        # launches can also be usable without a configured local profile.
        if not (
            getattr(args, "resume", None)
            or os.environ.get("HERMES_TUI_GATEWAY_URL", "").strip()
            or getattr(args, "base_url", None)
            or getattr(args, "api_key", None)
            or _has_any_provider_configured()
        ):
            _first_run_setup_guard(args)
            return 0
        return launch_from_args(args)
    except ImportError as exc:
        from hermes_constants import emit_partial_update_hint

        if emit_partial_update_hint(exc):
            return 1
        raise
