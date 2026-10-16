"""Outgoing webhooks: connect DuoSim to other systems (PLM, ticketing, Slack bridges, CI).

Each delivery is a JSON POST signed like Stripe's: header `DuoSim-Signature: t=<unix>,v1=<hex HMAC-SHA256 of
"<t>.<body>" with the endpoint secret>`.  Deliveries run on a small thread pool, are retried 3 times with
back-off, and the destination is re-checked against the SSRF policy before every attempt.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .billing import sign
from .core import SaaS
from .netguard import URLRejected, check_url

log = logging.getLogger("duosim.webhooks")
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="duosim-webhook")
RETRY_DELAYS = (0.0, 2.0, 10.0)


def _post(url: str, body: bytes, headers: dict[str, str]) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - URL policy checked
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def deliver(saas: SaaS, hook: dict[str, Any], event: str, data: dict[str, Any], post=_post, sleep=time.sleep) -> str:
    body = json.dumps(
        {"id": "evt_" + uuid.uuid4().hex[:20], "type": event, "created": int(time.time()), "data": data},
        separators=(",", ":"),
        default=str,
    ).encode()
    status = "not sent"
    for delay in RETRY_DELAYS:
        if delay:
            sleep(delay)
        try:
            check_url(hook["url"])
            code = post(
                hook["url"],
                body,
                {
                    "Content-Type": "application/json",
                    "User-Agent": "DuoSim-Webhooks/1",
                    "DuoSim-Event": event,
                    "DuoSim-Signature": sign(body, hook["secret"]),
                },
            )
            status = f"HTTP {code}"
            if 200 <= code < 300:
                break
        except URLRejected as e:
            status = f"blocked: {e}"
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            status = f"error: {type(e).__name__}"
    saas.webhook_result(hook["id"], status)
    return status


def emit(saas: SaaS, ws_id: str, event: str, data: dict[str, Any]) -> int:
    """Queue `event` for every active endpoint of the workspace subscribed to it.  Never raises."""
    try:
        hooks = saas.webhooks(ws_id, event, with_secret=True)
    except Exception as e:
        log.warning("webhook lookup failed: %s", e)
        return 0
    for h in hooks:
        _POOL.submit(deliver, saas, h, event, data)
    return len(hooks)
