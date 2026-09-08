"""Gateway service setup orchestration; runtime startup is separate."""

def ensure_gateway_service(context: str = "setup") -> bool:
    """Install and start a user-scope gateway service without prompting (``hermes setup``/``import``).
    A zero-platform gateway is a supported degraded mode (cron runs), so this never gates on messaging
    config. Never raises; True when a service is installed and running."""
    from hermes_cli.gateway import (
        print_info,
        supports_systemd_services,
        is_macos,
        is_windows,
        _is_service_running,
        _is_service_installed,
        has_conflicting_systemd_units,
        print_systemd_scope_conflict_warning,
        systemd_install,
        launchd_install,
        _gw_windows,
        print_success,
        systemd_start,
        launchd_start,
        UserSystemdUnavailableError,
        print_warning,
        _print_indented,
        SystemScopeRequiresRootError,
        _print_system_scope_remediation,
    )
    from hermes_constants import is_container
    if is_container():
        # Containers use restart policies, not service managers.
        print_info("Start the gateway to bring your bots online:")
        print_info("   hermes gateway run          # Run as container main process")
        print_info("")
        print_info("For automatic restarts, use a Docker restart policy:")
        print_info("   docker run --restart unless-stopped ...")
        return False

    supports_systemd = supports_systemd_services()
    if not (supports_systemd or is_macos() or is_windows()):
        print_info("  No supported service manager found on this host.")
        print_info("  Run the gateway in the foreground with: hermes gateway")
        return False

    try:
        if _is_service_running():
            return True
        if not _is_service_installed():
            if supports_systemd and has_conflicting_systemd_units():
                # Both units would fight over bot tokens; don't pile a fresh install onto a conflicted state.
                print_systemd_scope_conflict_warning()
                return False
            print_info("  Installing the gateway background service ...")
            if supports_systemd:
                systemd_install(force=False, non_interactive=True)
            elif is_macos():
                launchd_install(force=False)
            else:
                _gw_windows().install(force=False)  # Registers the Scheduled Task AND starts it.
                print_success("  Gateway service installed and started.")
                return True
        if supports_systemd:
            systemd_start()
        elif is_macos():
            launchd_start()
        else:
            _gw_windows().start()
        print_success("  Gateway service running (cron jobs + messaging platforms).")
        return True
    except UserSystemdUnavailableError as e:
        print_warning("  Could not reach user systemd to start the gateway service:")
        _print_indented(str(e), print_info)
    except SystemScopeRequiresRootError as e:
        print_warning(f"  Gateway service needs root for this scope: {e}")
        _print_system_scope_remediation("start")
    except SystemExit:
        # Some install/start paths sys.exit() on hard failures (temp-HOME guard); never abort setup/import.
        print_warning("  Gateway service install did not complete.")
        print_info("  You can retry manually: hermes gateway install")
    except Exception as e:
        print_warning(f"  Gateway service install failed: {e}")
        print_info("  You can retry manually: hermes gateway install")
    return False
