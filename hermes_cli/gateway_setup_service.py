"""Gateway service setup orchestration; runtime startup is separate."""

import sys

SERVICE_INSTALL_QUESTION = (
    "Install the gateway service so Hermes starts automatically at login "
    "and keeps scheduled jobs and messaging available?"
)


def record_service_choice(choice: str, config: dict | None = None) -> None:
    from hermes_cli.config import load_config, save_config

    if choice not in {"install", "decline"}:
        raise ValueError("service_install_choice must be install or decline")
    current = load_config()
    current.setdefault("gateway", {})["service_install_choice"] = choice
    save_config(current)
    # Setup keeps a working config which it saves again after this step.
    if config is not None:
        config.setdefault("gateway", {})["service_install_choice"] = choice


def wants_service_install(*, interactive: bool, install: bool = False,
                          config: dict | None = None) -> bool:
    from hermes_cli.config import load_config
    from hermes_cli.gateway import prompt_yes_no

    if install:
        return True
    if not interactive or not sys.stdin.isatty():
        return False
    choice = (config if config is not None else load_config()).get("gateway", {}).get("service_install_choice")
    if choice is None:
        if prompt_yes_no(SERVICE_INSTALL_QUESTION, False):
            return True
        record_service_choice("decline", config)
    return choice == "install"


def ensure_gateway_service(context: str = "setup", *, interactive: bool = False,
                           install: bool = False, config: dict | None = None) -> bool:
    """Start existing services; install only with explicit setup consent.

    Import and ordinary noninteractive callers cannot install, even with a saved
    install preference. False leaves foreground/unmanaged startup to the caller.
    """
    from hermes_cli import gateway as gw
    from hermes_constants import is_container

    if is_container():
        gw.print_info("Run hermes gateway run as the container main process; use a Docker restart policy for persistence.")
        return False
    supports_systemd = gw.supports_systemd_services()
    if not (supports_systemd or gw.is_macos() or gw.is_windows()):
        gw.print_info("No supported service manager found. Run: hermes gateway run")
        return False
    try:
        if gw._is_service_running():
            return True
        if not gw._is_service_installed():
            if context == "import" or not wants_service_install(
                interactive=interactive, install=install, config=config
            ):
                gw.print_info("No service installed. Run: hermes gateway run")
                gw.print_info("Without a service, scheduled jobs and messaging stop at logout/reboot; jobs cannot run while the host is off.")
                return False
            if supports_systemd and gw.has_conflicting_systemd_units():
                gw.print_systemd_scope_conflict_warning()
                return False
            if supports_systemd:
                gw.systemd_install(force=False, non_interactive=True)
            elif gw.is_macos():
                gw.launchd_install(force=False)
            else:
                gw._gw_windows().install(force=False)
            # Installers can refuse without raising (e.g. temporary-home guard).
            if not gw._is_service_installed():
                gw.print_warning("Gateway service install did not complete. Retry: hermes gateway install")
                return False
            record_service_choice("install", config)
        if supports_systemd:
            gw.systemd_start()
        elif gw.is_macos():
            gw.launchd_start()
        else:
            gw._gw_windows().start()
        gw.print_success("Gateway service running (cron jobs + messaging platforms).")
        return True
    except gw.UserSystemdUnavailableError as exc:
        gw.print_warning("Could not reach user systemd to start the gateway service:")
        gw._print_indented(str(exc), gw.print_info)
    except gw.SystemScopeRequiresRootError as exc:
        gw.print_warning(f"Gateway service needs root for this scope: {exc}")
        gw._print_system_scope_remediation("start")
    except SystemExit:
        gw.print_warning("Gateway service install did not complete. Retry: hermes gateway install")
    except Exception as exc:
        gw.print_warning(f"Gateway service setup failed: {exc}")
        gw.print_info("You can retry manually: hermes gateway install")
    return False
