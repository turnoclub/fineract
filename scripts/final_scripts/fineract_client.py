"""Dependency-free Fineract REST client (stdlib only) — hardened for bulk runs.

Used by every step in this pipeline. Talks to Fineract over HTTPS with basic auth
and the Fineract-Platform-TenantId header.

Resilience:
  - retries transient transport failures (dropped connections, timeouts,
    RemoteDisconnected, local ephemeral-port exhaustion / Errno 49) with backoff;
  - optional FINERACT_THROTTLE (seconds before each request) to cap the request
    rate so a fragile server is not overwhelmed.

Config via env (or pass explicitly to FineractClient):
  FINERACT_BASE_URL   e.g. https://10.120.139.62:8443/fineract-provider/api/v1
  FINERACT_TENANT     e.g. default
  FINERACT_USER       e.g. mifos
  FINERACT_PASSWORD   e.g. password
  FINERACT_VERIFY_TLS "true" to verify the cert (default false for the dev box)
  FINERACT_THROTTLE   seconds to sleep before each request (default 0)
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DATE_FORMAT = "dd MMMM yyyy"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


class FineractError(RuntimeError):
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
    throttle: float = 0.0
    RETRIES: int = 4

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
                timeout=int(os.environ.get("FINERACT_TIMEOUT", "30") or 30),
            )
        except KeyError as missing:
            raise SystemExit(f"Missing env var {missing}. Set FINERACT_BASE_URL/TENANT/USER/PASSWORD.")

    def _ctx(self):
        if self.verify_tls:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _headers(self):
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Fineract-Platform-TenantId": self.tenant,
                "Content-Type": "application/json", "Accept": "application/json"}

    def _request(self, method: str, path: str, body: Any | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        if self.throttle:
            time.sleep(self.throttle)
        last = None
        for attempt in range(self.RETRIES):
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx()) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                raw = e.read().decode()
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw
                raise FineractError(e.code, parsed, method, path) from None
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as e:
                last = getattr(e, "reason", e)
                if attempt < self.RETRIES - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise FineractError(0, f"connection error after {self.RETRIES} attempts: {last}", method, path)

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body=None):
        return self._request("POST", path, body if body is not None else {})

    def batch(self, requests, enclosing_transaction=False):
        """POST a list of BatchRequest dicts to /batches; returns the BatchResponse list."""
        path = f"batches?enclosingTransaction={'true' if enclosing_transaction else 'false'}"
        return self._request("POST", path, requests)


# -- shared helpers ----------------------------------------------------------


def iso_to_fineract(iso: str) -> str:
    """'2026-04-05' -> '05 April 2026'."""
    y, m, d = (int(x) for x in iso.strip().split("-"))
    return f"{d:02d} {MONTHS[m - 1]} {y}"


def arr_to_fineract(a) -> str:
    """Fineract date array [yyyy,m,d] -> '05 April 2026'."""
    return f"{a[2]:02d} {MONTHS[a[1] - 1]} {a[0]}"


def is_duplicate_external_id(err: "FineractError") -> bool:
    body = err.body
    text = (body if isinstance(body, str) else json.dumps(body)).lower()
    return any(m in text for m in ("duplicate.externalid", "already exists", "must.be.unique", "external_id_unique"))


def repayment_body(date, amount, payment_type_id=None, payment_ref=None, external_id=None) -> dict:
    """Repayment body; receiptNumber is capped at 50 chars (trim from start)."""
    b = {"transactionDate": date, "transactionAmount": amount, "dateFormat": DATE_FORMAT, "locale": "en"}
    if payment_type_id not in (None, ""):
        b["paymentTypeId"] = int(payment_type_id)
    if payment_ref not in (None, ""):
        b["receiptNumber"] = str(payment_ref)[-50:]
    if external_id not in (None, ""):
        b["externalId"] = str(external_id)
    return b


def resolve_loan_by_lms(client: "FineractClient", lms: str):
    """Return the loan dict for externalId=LMS, or None if it doesn't exist (404)."""
    try:
        return client.get(f"loans/external-id/{lms}")
    except FineractError as e:
        if e.status == 404:
            return None
        raise
