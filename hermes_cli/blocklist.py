"""CLI helpers for user-managed web policy blocklists."""

from __future__ import annotations

from hermes_cli.config import load_config, save_config
from tools.url_policy import explain_url, normalize_policy_host, refresh_subscriptions


def _load_web_policy() -> dict:
    cfg = load_config()
    policy = cfg.setdefault(
        "web_policy",
        {
            "enabled": True,
            "local_blocks": [],
            "local_exceptions": [],
            "subscriptions": [],
            "audit_blocked_attempts": False,
        },
    )
    return cfg


def _find_subscription_record(result: dict, source: str, list_id: str) -> dict | None:
    for entry in result.get("subscriptions", []) or []:
        if entry.get("source") == source and entry.get("list_id") == list_id:
            return entry
    return None


def blocklist_command(args) -> None:
    action = getattr(args, "blocklist_action", None)

    if action == "add":
        cfg = _load_web_policy()
        host = normalize_policy_host(getattr(args, "host", ""))
        if not host:
            print("Error: invalid host")
            return
        blocks = cfg["web_policy"].setdefault("local_blocks", [])
        if host in blocks:
            print(f"Block already present: {host}")
            return
        blocks.append(host)
        save_config(cfg)
        print(f"Added block: {host}")
        return

    if action == "remove":
        cfg = _load_web_policy()
        host = normalize_policy_host(getattr(args, "host", ""))
        if not host:
            print("Error: invalid host")
            return
        blocks = cfg["web_policy"].setdefault("local_blocks", [])
        updated = [item for item in blocks if item != host]
        if len(updated) == len(blocks):
            print(f"Block not found: {host}")
            return
        cfg["web_policy"]["local_blocks"] = updated
        save_config(cfg)
        print(f"Removed block: {host}")
        return

    if action == "subscribe":
        cfg = _load_web_policy()
        source = str(getattr(args, "source", "") or "").strip()
        list_id = str(getattr(args, "list_id", "") or "").strip()
        if not source or not list_id:
            print("Error: subscribe requires source and list_id")
            return
        subscriptions = cfg["web_policy"].setdefault("subscriptions", [])
        entry = {"source": source, "list_id": list_id}
        already_present = entry in subscriptions
        if not already_present:
            subscriptions.append(entry)
            save_config(cfg)
        result = refresh_subscriptions()
        subscription = _find_subscription_record(result, source, list_id)
        prefix = "Already subscribed" if already_present else "Subscribed"
        if subscription and subscription.get("error"):
            print(
                f"{prefix} to {list_id} from {source}, but refresh failed: "
                f"{subscription['error']}"
            )
            return
        print(f"{prefix} to {list_id} from {source}")
        return

    if action == "unsubscribe":
        cfg = _load_web_policy()
        source = str(getattr(args, "source", "") or "").strip()
        list_id = str(getattr(args, "list_id", "") or "").strip()
        subscriptions = cfg["web_policy"].setdefault("subscriptions", [])
        updated = [
            item
            for item in subscriptions
            if not (item.get("source") == source and item.get("list_id") == list_id)
        ]
        if len(updated) == len(subscriptions):
            print(f"Subscription not found: {list_id} from {source}")
            return
        cfg["web_policy"]["subscriptions"] = updated
        save_config(cfg)
        print(f"Unsubscribed {list_id} from {source}")
        return

    if action == "why":
        url = str(getattr(args, "url", "") or "").strip()
        if not url:
            print("Error: why requires a URL")
            return
        decision = explain_url(url)
        print(f"allowed: {decision.allowed}")
        print(f"decision_source: {decision.decision_source}")
        print(f"reason: {decision.reason}")
        if decision.rule_identity:
            print(f"rule_identity: {decision.rule_identity}")
        if decision.source_identity:
            print(f"source_identity: {decision.source_identity}")
        if decision.source_title:
            print(f"source_title: {decision.source_title}")
        return

    if action == "update":
        result = refresh_subscriptions()
        error_count = sum(1 for entry in result.get("subscriptions", []) or [] if entry.get("error"))
        print(
            f"Refreshed {len(result.get('subscriptions', []))} subscription(s); "
            f"{result.get('rule_count', 0)} rule(s) active; "
            f"{error_count} error(s)."
        )
        return

    if action == "list":
        cfg = _load_web_policy()
        policy = cfg["web_policy"]
        print("Local blocks:")
        blocks = policy.get("local_blocks", [])
        if not blocks:
            print("  (none)")
        for host in blocks:
            print(f"  - {host}")
        print("Local exceptions:")
        exceptions = policy.get("local_exceptions", [])
        if not exceptions:
            print("  (none)")
        for host in exceptions:
            print(f"  - {host}")
        print("Subscriptions:")
        subscriptions = policy.get("subscriptions", [])
        if not subscriptions:
            print("  (none)")
        for entry in subscriptions:
            print(f"  - {entry.get('list_id')} @ {entry.get('source')}")
        return

    print("Error: unknown blocklist action")
