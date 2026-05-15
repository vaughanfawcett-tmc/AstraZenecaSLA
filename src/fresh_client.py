"""Freshdesk API client for pulling ticket descriptions + conversation transcripts.

Used by the Tier-2 classifier when the export fields don't carry enough context
to classify a ticket. Configured via two env vars (both required to enable):

    FRESHDESK_DOMAIN   e.g. "yourcompany" or "yourcompany.freshdesk.com"
    FRESHDESK_API_KEY  the user's API key (Profile Settings → API key in Freshdesk)

The API key user must have ticket-view permission on AstraZeneca tickets.

Auth: HTTP Basic with (api_key, "X") — Freshdesk's documented pattern.
Rate limits: plan-dependent (50–700/min). We honour 429 Retry-After once.
Cache: per-ticket JSON at data/fresh_cache/<ticket_id>.json. Closed tickets
are effectively immutable, so re-runs are free after the first hit.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE_DIR = ROOT / "data" / "fresh_cache"


def is_available() -> bool:
    """True if both FRESHDESK_DOMAIN and FRESHDESK_API_KEY are set."""
    return bool(os.environ.get("FRESHDESK_DOMAIN")) and bool(os.environ.get("FRESHDESK_API_KEY"))


def _normalise_domain(domain: str) -> str:
    d = domain.strip().rstrip("/")
    if d.startswith("http://") or d.startswith("https://"):
        d = d.split("://", 1)[1]
    if not d.endswith(".freshdesk.com"):
        d = f"{d}.freshdesk.com"
    return d


class FreshClient:
    """Tiny Freshdesk client. Stateless across requests apart from the disk cache."""

    def __init__(
        self,
        domain: Optional[str] = None,
        api_key: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        timeout: float = 20.0,
    ):
        domain = domain or os.environ.get("FRESHDESK_DOMAIN", "")
        api_key = api_key or os.environ.get("FRESHDESK_API_KEY", "")
        if not domain or not api_key:
            raise ValueError("FRESHDESK_DOMAIN and FRESHDESK_API_KEY must be set")

        self.host = _normalise_domain(domain)
        self.base_url = f"https://{self.host}/api/v2"
        token = base64.b64encode(f"{api_key}:X".encode()).decode()
        self.auth_header = f"Basic {token}"
        self.timeout = timeout
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _request(self, path: str) -> Optional[dict | list]:
        """GET <base>/<path> with one retry on 429. Returns parsed JSON or None on 4xx (except 429)."""
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            req = urllib.request.Request(url, headers={
                "Authorization": self.auth_header,
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    retry_after = float(e.headers.get("Retry-After", "2"))
                    time.sleep(min(retry_after, 30.0))
                    continue
                if e.code in (401, 403):
                    raise PermissionError(f"Freshdesk auth failed ({e.code}) for {path}") from e
                if e.code == 404:
                    return None
                raise
        return None

    def _cache_path(self, ticket_id) -> Path:
        return self.cache_dir / f"{ticket_id}.json"

    def fetch_ticket(self, ticket_id) -> Optional[dict]:
        """Fetch description + conversations for a ticket. Cached on disk.

        Returns a dict shaped:
            {"description_text": str, "conversations": [str, ...]}
        or None if the ticket can't be found or ticket_id is empty.
        """
        if ticket_id is None or str(ticket_id).strip() == "":
            return None
        cache = self._cache_path(ticket_id)
        if cache.exists():
            try:
                return json.loads(cache.read_text())
            except json.JSONDecodeError:
                pass  # corrupt cache entry — refetch

        # Bare /tickets/{id} returns description + description_text by default.
        # The previous `?include=description` was rejected by the API as an
        # invalid include value (valid: conversations, requester, company,
        # stats, sla_policy).
        ticket = self._request(f"/tickets/{ticket_id}")
        if ticket is None:
            return None

        convos_raw = self._request(f"/tickets/{ticket_id}/conversations") or []
        # body_text is the plain-text version; body is HTML. Prefer body_text.
        conversations = [
            (c.get("body_text") or "").strip()
            for c in convos_raw
            if c.get("body_text")
        ]
        out = {
            "description_text": (ticket.get("description_text") or "").strip(),
            "conversations": conversations,
        }
        cache.write_text(json.dumps(out, ensure_ascii=False))
        return out

    def get_context_text(self, ticket_id, max_chars: int = 3500) -> Optional[str]:
        """Return a single string suitable for appending to an LLM prompt.

        Concatenates description + conversation replies in order, capped at
        max_chars to keep token usage sane. Returns None if nothing fetchable.
        """
        data = self.fetch_ticket(ticket_id)
        if not data:
            return None
        parts = []
        if data.get("description_text"):
            parts.append(f"DESCRIPTION:\n{data['description_text']}")
        for i, body in enumerate(data.get("conversations") or [], start=1):
            parts.append(f"REPLY {i}:\n{body}")
        if not parts:
            return None

        joined = "\n\n".join(parts)
        if len(joined) > max_chars:
            joined = joined[:max_chars] + "\n…[truncated]"
        return joined
