"""Record the tabs a ``browser_exec`` call creates, from inside the browser-harness process.

browser_exec installs this over ``browser_harness.helpers.cdp`` (see ``browser_use_shot.harness_command``)
before ``browser_harness.run`` star-imports the helpers, so the model's code, ``new_tab()`` and
``close_tab()`` all go through the wrapper. Each successful ``Target.createTarget`` reply's ``targetId``
is written to the per-call 0600 report file named by ``BH_HERMES_TAB_REPORT`` (set by browser_exec only
on a shared browser; unset, nothing is installed), and a successful ``Target.closeTarget`` takes it out.
The report is rewritten after every event, so a call that is killed or times out still says what it
opened. Hermes merges the report into ``tools.browser_session_owned_tabs`` after the call and closes
only those tabs when the session ends: a tab the call merely switched to is never recorded.

Loaded by file path inside the harness interpreter, which has no Hermes checkout on ``sys.path``: this
module imports only the standard library.
"""

from __future__ import annotations

import functools
import json
import os
from typing import Any

REPORT_ENV = "BH_HERMES_TAB_REPORT"


def _persist(path: str, state: dict[str, list[str]]) -> None:
    pending = path + ".tmp"
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(state, stream)
    os.replace(pending, path)


def install(helpers: Any) -> None:
    """Wrap ``helpers.cdp`` so created and closed target ids land in ``$BH_HERMES_TAB_REPORT``."""
    report_path = os.environ.get(REPORT_ENV)
    if not report_path:
        return
    original = helpers.cdp
    state: dict[str, list[str]] = {"created": [], "closed": []}

    @functools.wraps(original)
    def cdp(method, *args, **kwargs):
        result = original(method, *args, **kwargs)  # a CDP error raises: nothing is recorded
        if method == "Target.createTarget":
            target = (result or {}).get("targetId") if isinstance(result, dict) else None
            if isinstance(target, str) and target:
                state["created"].append(target)
                _persist(report_path, state)
        elif method == "Target.closeTarget":
            target = kwargs.get("targetId")
            if isinstance(target, str) and target:
                if target in state["created"]:
                    state["created"].remove(target)
                else:
                    state["closed"].append(target)  # opened by an earlier call of this session
                _persist(report_path, state)
        return result

    helpers.cdp = cdp
