"""Minimal dependency-free Fineract REST client (stdlib urllib only).

Shared by reconcile.py (monthly bounce reconciliation) and migrate.py (old-case
ledger replay). Talks to a Fineract provider over HTTPS with basic auth and the
Fineract-Platform-TenantId header. TLS verification is OFF by default to tolerate
the self-signed dev certificate -- override for any non-dev target.

Config via env vars (or pass explicitly to FineractClient):
  FINERACT_BASE_URL   e.g. https://10.120.139.62:8443/fineract-provider/api/v1
  FINERACT_TENANT     e.g. default
  FINERACT_USER       e.g. mifos
  FINERACT_PASSWORD   e.g. password
  FINERACT_VERIFY_TLS "true" to enable cert verification (default false)
"""

from __future__ import annotations

import base64
import json
import http.client
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class FineractError(RuntimeError):
    """Raised on a non-2xx response; carries status and parsed/raw body."""

    def __init__(self, status: int, body: Any, method: str, path: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")


@dataclass
class FineractClient:
    base_url: str
    tenant: str
    user: str
    password: str
    verify_tls: bool = False
    timeout: int = 30
    throttle: float = 0.0  # seconds to sleep before each request (rate cap for bulk runs)

    @classmethod
    def from_env(cls) -> "FineractClient":
        try:
            return cls(
                base_url=os.environ["FINERACT_BASE_URL"].rstrip("/"),
                tenant=os.environ["FINERACT_TENANT"],
                user=os.environ["FINERACT_USER"],
                password=os.environ["FINERACT_PASSWORD"],
                verify_tls=os.environ.get("FINERACT_VERIFY_TLS", "false").lower() == "true",
                throttle=float(os.environ.get("FINERACT_THROTTLE", "0") or 0),
            )
        except KeyError as missing:
            raise SystemExit(
                f"Missing required env var {missing}. "
                "Set FINERACT_BASE_URL, FINERACT_TENANT, FINERACT_USER, FINERACT_PASSWORD."
            )

    # -- internals -----------------------------------------------------------

    def _ctx(self) -> ssl.SSLContext | None:
        if self.verify_tls:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Fineract-Platform-TenantId": self.tenant,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # Transient connection failures retried before giving up (server drops, timeouts).
    RETRIES = 4

    def _request(self, method: str, path: str, body: Any | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        if self.throttle:
            time.sleep(self.throttle)  # cap connection rate -> avoid ephemeral-port exhaustion
        last = None
        for attempt in range(self.RETRIES):
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx()) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                # A real HTTP response (4xx/5xx) -- not transient, do not retry.
                raw = e.read().decode()
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw
                raise FineractError(e.code, parsed, method, path) from None
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as e:
                # Dropped/refused connection, timeout, RemoteDisconnected, or local socket
                # exhaustion (EADDRNOTAVAIL/Errno 49): retry with backoff. OSError covers the
                # bare-errno cases that ConnectionError misses.
                last = getattr(e, "reason", e)
                if attempt < self.RETRIES - 1:
                    time.sleep(1.5 * (attempt + 1))
        # Exhausted retries: surface as FineractError(0) so per-item handlers skip-and-report
        # instead of crashing the whole run.
        raise FineractError(0, f"connection error after {self.RETRIES} attempts: {last}", method, path)

    # -- public API ----------------------------------------------------------

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: Any | None = None) -> Any:
        return self._request("POST", path, body if body is not None else {})

    def put(self, path: str, body: Any) -> Any:
        return self._request("PUT", path, body)

    def batch(self, requests: list[dict], enclosing_transaction: bool = False) -> list[dict]:
        """POST a list of BatchRequest dicts to /batches.

        Each entry: {requestId, relativeUrl, method, headers?, body?, reference?}.
        Returns the list of BatchResponse dicts (statusCode + body per requestId).
        With enclosing_transaction=False (default) a failed sub-request does not
        roll back the others -- inspect each statusCode and retry the failures.
        """
        path = f"batches?enclosingTransaction={'true' if enclosing_transaction else 'false'}"
        return self._request("POST", path, requests)


# -- shared helpers ----------------------------------------------------------


def find_bounce_charge(
    client: FineractClient, loan_id: int, charge_name: str = "Bounce Charges"
) -> dict | None:
    """Return the open/partially-paid bounce loan-charge dict for a loan, or None.

    Picks a penalty charge whose name matches and that still has something to act
    on (outstanding > 0, i.e. not already fully paid or fully waived). The dict's
    'id' is the loanChargeId; 'amountPaid' is the refund amount for an uncollect.
    """
    charges = client.get(f"loans/{loan_id}/charges") or []
    candidates = [
        c
        for c in charges
        if c.get("penalty")
        and charge_name.lower() in (c.get("name") or "").lower()
        and float(c.get("amountOutstanding") or 0) > 0
    ]
    if not candidates:
        return None
    # Earliest due first, so monthly reconciliation acts on the oldest open bounce.
    candidates.sort(key=lambda c: tuple(c.get("dueDate") or []))
    return candidates[0]


def repayment_body(
    date: str,
    amount,
    payment_type_id=None,
    payment_ref=None,
    external_id=None,
) -> dict:
    """Build a repayment request body, attaching payment id fields when provided.

    - payment_type_id -> paymentTypeId (FK to m_payment_type; the payment method)
    - payment_ref     -> receiptNumber (the NACH/UTR/txn reference, recorded only)
    - external_id     -> externalId (the transaction's unique id; Fineract enforces
                         uniqueness on it, so passing your payment id here makes the
                         repayment idempotent -- a re-post fails with value.must.be.unique)
    """
    body: dict = {
        "transactionDate": date,
        "transactionAmount": amount,
        "dateFormat": "dd MMMM yyyy",
        "locale": "en",
    }
    if payment_type_id not in (None, ""):
        body["paymentTypeId"] = int(payment_type_id)
    if payment_ref not in (None, ""):
        # receiptNumber is capped at 50 chars in Fineract; trim from the start
        # (keep the rightmost 50) so long references like cash-deposit strings post.
        body["receiptNumber"] = str(payment_ref)[-50:]
    if external_id not in (None, ""):
        body["externalId"] = str(external_id)
    return body


def is_duplicate_external_id(err: "FineractError") -> bool:
    """True if a FineractError means the transaction externalId already exists.

    Lets callers treat an already-posted transaction as 'already done' on re-runs.
    Fineract reports this two ways depending on the path:
      - repayment: error.msg.loan.repayment.duplicate.externalId / "...already exists"
      - generic save: external_id_UNIQUE constraint -> externalId value.must.be.unique
    """
    body = err.body
    text = (body if isinstance(body, str) else __import__("json").dumps(body)).lower()
    return any(
        marker in text
        for marker in ("duplicate.externalid", "already exists", "must.be.unique", "external_id_unique")
    )
