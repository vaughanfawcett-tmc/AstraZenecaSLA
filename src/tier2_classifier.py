"""Tier-2 LLM categoriser using Claude Haiku 4.5 via OpenRouter.

OpenRouter exposes an OpenAI-compatible chat-completions endpoint that proxies
through to Anthropic. Two upstream features we still get to use end-to-end:

* **Anthropic prompt caching** — OpenRouter passes the `cache_control` marker
  on a system content part through to Anthropic. The (large, stable) classifier
  prompt is paid for once per 5-minute TTL and read cheaply for the rest of the
  run.
* **OpenAI-style tool calling** — used as a structured-output channel. We force
  the single `classify_ticket` tool with `tool_choice` so every reply is valid
  schema-checked JSON.

Set OPENROUTER_API_KEY to enable.
"""
from __future__ import annotations
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

try:
    from openai import AzureOpenAI, OpenAI
except ImportError:
    AzureOpenAI = None  # type: ignore[assignment]
    OpenAI = None  # type: ignore[assignment]

from categories import CATEGORIES
import fresh_client


# Three providers supported, in priority order when multiple keys are set:
#   - AZURE_OPENAI_API_KEY (+ AZURE_OPENAI_ENDPOINT) → Azure OpenAI (gpt-4o by default)
#   - OPENAI_API_KEY  → OpenAI direct (gpt-4o-mini by default)
#   - OPENROUTER_API_KEY → OpenRouter to Claude Haiku 4.5
# Override the deployment/model with TIER2_MODEL.
def _provider() -> str:
    if os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT"):
        return "azure"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    return ""


_DEFAULT_AZURE_MODEL = "gpt-4o"
_DEFAULT_AZURE_API_VERSION = "2025-04-01-preview"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"


@dataclass
class Tier2Result:
    sub_category: str
    category: str
    confidence: float
    reasoning: str


def _build_system_prompt() -> str:
    """A long, stable system prompt — cacheable across all tickets in a run.

    Includes the closed category list, decision rules, and worked examples.
    """
    cat_list = "\n".join(f"  - Sub Category: {s!r:<30}  Category: {c!r}" for s, c in CATEGORIES)

    return f"""You are an expert ticket classifier for AstraZeneca's TMC (Total Mileage Capture) support service.

Your job is to read a Freshdesk support ticket and assign it to ONE of the AstraZeneca client's official (Sub Category, Category) pairs from the closed list below. AstraZeneca uses these categories for monthly SLA reporting and they MUST come from this closed list — never invent new categories.

# Closed list of valid (Sub Category, Category) pairs

{cat_list}

# Domain context

TMC is a fleet-vehicle mileage capture service. Drivers (AstraZeneca employees with company cars) submit monthly mileage. The TMC support team (the client of this report) handles tickets via Freshdesk. Common ticket scenarios:

- **Mileage submission** — driver sends their kilometre/mile reading at month-end. Subjects often contain "Km", "Kilométrage", "Inserimento km", "Chilometraggio", "mileage", "Conversation with <name>".
- **Account access / login** — driver locked out of the TMC portal/app. Subjects: "Compte bloqué", "Password reset", "Login issue", "Cannot access app".
- **Vehicle changes** — driver got a new car or returned one. Subjects often mention vehicle make/model or registration plate.
- **Fleet Logistics updates** — automated notifications from FleetLogistics about new/leaving employees, line manager changes, mobile number updates. Subjects often start with `[<digits>]` and contain "Astra Zeneca:", "Amendment to be made", or similar bracketed reference numbers.
- **Audit responses** — driver responding to a TMC audit query about high mileage, missing fuel receipts, multiple fuel transactions on one day, etc.
- **Auto-close reminders** — the system pinged a driver who hadn't submitted at month-close; driver replied with their mileage.
- **Card queries** — questions about the Citi fuel card (lost, ordering, transactions).
- **Payroll / EV** — questions about EV charging reimbursement or payroll calculations involving fuel cards.

# Decision rules (apply in order)

1. **`Sub Category: Mileage Entry / Category: Employee Sent Mileage To TMC`** — the driver is submitting their mileage reading. Look for numeric km/mile values, month references ("December", "fine mese", "dicembre"), or chat conversations where a driver provides a reading. This is the single most common category.

2. **`Sub Category: Mileage Entry / Category: Odometer Reading Correction`** — driver is correcting a previously-submitted reading (often after the system flagged a discrepancy or the driver realised they entered private vs business mileage incorrectly).

3. **`Sub Category: Account Access / Category: Password Reset`** — driver cannot log in / needs credentials resent. Strong signals: "Compte bloqué", "password", "blocked account", "cannot access app", "support needed: login unavailable", "unable to log in", "logon details", "logon issue". When the Freshdesk fields say `query_regarding=Account Details / type=Resent Logon Details`, this is the **dominant** client label (~60% of those tickets), so pick it unless the subject clearly shows a different issue (mileage submission, vehicle change, etc.). Otherwise, do NOT use this bucket purely because the app isn't working — see Account Check below.

4. **`Sub Category: Account Details / Category: Fleet Logistics Update`** — automated FleetLogistics notification. Strong signals: subject begins with `[<digits>]` or `[<digits>][<digits>]`, contains "Astra Zeneca: New", "Astra Zeneca: Leaver", "Astra Zeneca: Vehicle", "Amendment to be made". These are bulk system-generated tickets, NOT a driver query.

5. **`Sub Category: Account Details / Category: Employee Vehicle Change Request`** — driver is asking about *getting* a new vehicle, *returning* one, or notifying TMC of a vehicle swap (different from the automated FleetLogistics update — this is a driver-initiated request). **Important**: subjects that prefix `KM <plate>` / `Km <plate>` / `Chilometraggio auto <plate>` or end with a vehicle plate after a number are **mileage submissions** that happen to mention the vehicle ID — classify those as `Mileage Entry / Employee Sent Mileage To TMC`, not vehicle change.

6. **`Sub Category: Account Details / Category: Account Check`** — generic "please check my account / something looks wrong / I cannot do X in the app / unlock the period for me" where the issue is account-state-related but doesn't fit a more specific bucket. For Freshdesk combos that are split across multiple buckets, this is the **fallback when no stronger signal is present**:
   - `(Technical Issue, App Support)` is **mixed** in client data — 34% Account Check, 26% Mileage Entry, 18% Password Reset, 13% Auto Close Reminder. Pick from the **subject content**: a credential / login keyword → Password Reset; a mileage number or month reference → Mileage Entry; generic "app isn't working" / "support needed" → Account Check.
   - `(Technical Issue, Milcap Support)` follows the same pattern — read the subject.
   - For ambiguous generic subjects like `"Conversation with User"` / `"Call Back Requested by <name>"` with no qr/type signal, default to Account Check unless the subject contains a clear mileage number, vehicle plate, or login keyword.

7. **`Sub Category: Account Details / Category: Line Manager Reporting`** — query about line manager assignment / reporting structure.

8. **`Sub Category: Account Details / Category: AZ Update`** — internal AstraZeneca-side update that isn't the standard FleetLogistics flow (e.g. cost centre change, scheme change, internal ID update).

9. **`Sub Category: Auto Close Reminder / Category: Employee Sent Mileage To TMC`** — **use sparingly**. This bucket is reserved for tickets generated purely by the auto-close reminder workflow with no manual cut-off intervention. **DO NOT pick Auto Close Reminder when the Freshdesk fields say `Missed Cut-Off / Pushed Back` or `Closing Period / After Month Close`** — those qr/type values mean a TMC analyst manually pushed the ticket back to the driver for a reading, which the client classifies as `Mileage Entry / Employee Sent Mileage To TMC` even if the subject quotes the auto-close reminder email ("RE: TMC Close Off Reminder", "Rappel de Cloture", "Periodo chiuso", "Erinnerung", "TMC-Eintrag versäumt"). The subject alone is NOT enough to put this in Auto Close Reminder — the Freshdesk fields must be empty or different from the Pushed-Back / After-Month-Close pair.

10. **`Sub Category: Auto Close Reminder / Category: Odometer Reading Correction`** — same narrow trigger as above (auto-close reminder, no manual intervention) but the driver is correcting a prior reading.

11. **`Sub Category: Audit / Category: Multi Fuel`** — driver responding about multiple fuel transactions flagged by the audit system. Freshdesk often tags these as `(Audit, Velocity)`.

12. **`Sub Category: Audit / Category: Missing Fuel`** — audit query about a missing fuel receipt or expected transaction.

13. **`Sub Category: Audit / Category: High Mileage (Total)`** — audit flagged unusually high total monthly mileage; this is the driver's response.

14. **`Sub Category: Card Query / Category: Citi bank card query`** — anything about the Citi fuel card.

15. **`Sub Category: Payroll / Category: Payroll Query`** — question about payroll calculation, reimbursement amounts.

16. **`Sub Category: App / Category: EV Tariff informantion`** — question about EV charging tariffs / reimbursement rates (note client's spelling "informantion" — preserve it).

# Worked examples

INPUT: subject="Km dicembre 2025", source="Email", query_regarding="", type=""
→ classify_ticket(sub_category="Mileage Entry", category="Employee Sent Mileage To TMC", confidence=0.95, reasoning="Italian for 'December kilometres' — clear monthly mileage submission.")

INPUT: subject="Compte bloqué", source="Email", query_regarding="Missed Cut-Off", type="Pushed Back"
→ classify_ticket(sub_category="Account Access", category="Password Reset", confidence=0.9, reasoning="French for 'blocked account' — login issue, not mileage despite the Freshdesk Missed Cut-Off tag.")

INPUT: subject="[46484890]Astra Zeneca: Leaver", source="Email", query_regarding="Updates", type="Leaver"
→ classify_ticket(sub_category="Account Details", category="Fleet Logistics Update", confidence=0.99, reasoning="Bracketed reference + 'Astra Zeneca: Leaver' is a FleetLogistics automated notification.")

INPUT: subject="business KMs for cut-off date missed", source="Email", query_regarding="Missed Cut-Off", type="Pushed Back"
→ classify_ticket(sub_category="Mileage Entry", category="Employee Sent Mileage To TMC", confidence=0.9, reasoning="qr=Missed Cut-Off + type=Pushed Back is the manual push-back path — client bucket is Mileage Entry, not Auto Close Reminder, even though the subject sounds reminder-flavoured.")

INPUT: subject="Sostituzione Auto", source="Email", query_regarding="Updates", type="Vehicle"
→ classify_ticket(sub_category="Account Details", category="Employee Vehicle Change Request", confidence=0.85, reasoning="Italian for 'vehicle replacement' — driver-initiated request about a new vehicle.")

INPUT: subject="KM Mustang GS052XF", source="Email", query_regarding="Mileage Claim", type="Assistance Entering Mileage"
→ classify_ticket(sub_category="Mileage Entry", category="Employee Sent Mileage To TMC", confidence=0.9, reasoning="'KM <plate>' is a mileage submission with the vehicle plate appended — not a vehicle change request.")

INPUT: subject="TMC app not accessible", source="Email", query_regarding="Technical Issue", type="App Support"
→ classify_ticket(sub_category="Account Details", category="Account Check", confidence=0.55, reasoning="Generic 'app not working' subject with no login keyword or mileage — falls into the Account Check fallback for the mixed App Support bucket.")

INPUT: subject="Compte bloqué - urgent", source="Email", query_regarding="Account Details", type="Resent Logon Details"
→ classify_ticket(sub_category="Account Access", category="Password Reset", confidence=0.9, reasoning="Resent Logon Details combo is majority Password Reset (~60%), and the subject directly says the account is blocked — credential issue, not a mileage submission.")

INPUT: subject="Conversation with User", source="Chat", query_regarding="", type=""
→ classify_ticket(sub_category="Account Details", category="Account Check", confidence=0.5, reasoning="Generic placeholder subject with no Freshdesk fields — default bucket per client convention.")

INPUT: subject="RE: TMC Close Off Reminder", source="Email", query_regarding="", type=""
→ classify_ticket(sub_category="Auto Close Reminder", category="Employee Sent Mileage To TMC", confidence=0.85, reasoning="Reply to the auto-close reminder with no manual push-back marker — narrow ACR bucket applies.")

If a ticket genuinely doesn't fit any category cleanly, pick the closest match and lower the confidence to 0.4 or below — that signals the row should be reviewed manually.
"""


# OpenAI-style tool used as a structured-output channel.
_CLASSIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "classify_ticket",
        "description": "Record the chosen (Sub Category, Category) pair, confidence, and one-line reasoning for this Freshdesk ticket.",
        "parameters": {
            "type": "object",
            "properties": {
                "sub_category": {
                    "type": "string",
                    "enum": sorted({s for s, _ in CATEGORIES}),
                    "description": "Must be one of the Sub Category values from the closed list in the system prompt.",
                },
                "category": {
                    "type": "string",
                    "enum": sorted({c for _, c in CATEGORIES}),
                    "description": "Must be one of the Category values; must be a valid pair with the chosen Sub Category.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "0.0–1.0 — drop below 0.6 if the ticket is ambiguous and should be eyeballed by a human.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One short sentence explaining the classification.",
                },
            },
            "required": ["sub_category", "category", "confidence", "reasoning"],
        },
    },
}


def is_available() -> bool:
    """True if the OpenAI SDK is installed AND at least one provider key is set."""
    return OpenAI is not None and bool(_provider())


def _make_client():
    # 60s per request, 5 retries on 429. The SDK honours the `Retry-After`
    # header so the added latency is bounded by the rate-limit window, not by
    # exponential blowup. (The original 1-retry/no-timeout default caused a
    # 2hr+ hang on a silently-dropped TCP connection during the first 267-row
    # Jan run — the 60s timeout is what protects against that, not the retry
    # count.) All three providers (Azure, OpenAI direct, OpenRouter) hit 429
    # under sustained load on default tiers; only the Retry-After window
    # differs (ms for OpenAI, ~60s for Azure).
    p = _provider()
    max_retries = int(os.environ.get("TIER2_MAX_RETRIES", "5"))
    if p == "azure":
        return AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", _DEFAULT_AZURE_API_VERSION),
            timeout=60.0,
            max_retries=max_retries,
        )
    if p == "openai":
        return OpenAI(
            api_key=os.environ["OPENAI_API_KEY"],
            timeout=60.0,
            max_retries=max_retries,
        )
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
        timeout=60.0,
        max_retries=max_retries,
        default_headers={
            "HTTP-Referer": "https://tmc.local/astrazeneca-sla",
            "X-Title": "AstraZeneca SLA Report Builder",
        },
    )


def _model() -> str:
    override = os.environ.get("TIER2_MODEL")
    if override:
        return override
    p = _provider()
    if p == "azure":
        return _DEFAULT_AZURE_MODEL
    if p == "openai":
        return _DEFAULT_OPENAI_MODEL
    return _DEFAULT_OPENROUTER_MODEL


def classify_one(
    client,
    system_messages: list[dict],
    ticket: dict,
    fresh: "fresh_client.FreshClient | None" = None,
    fetch_stats: dict | None = None,
) -> Tier2Result:
    """Classify a single ticket. The system prompt is cached on first call of the run.

    If ``fresh`` is provided, the client is used to pull the ticket's description
    and conversation transcript from Freshdesk and append it to the LLM prompt —
    addressing the brief's "look up case in Fresh when export is unclear" step.
    Failures are swallowed so a Fresh outage never breaks the classification run.
    """
    user_content = (
        f"subject: {ticket.get('subject') or ''!r}\n"
        f"source: {ticket.get('source') or ''!r}\n"
        f"query_regarding: {ticket.get('query_regarding') or ''!r}\n"
        f"type: {ticket.get('type') or ''!r}\n"
        f"full_name: {ticket.get('full_name') or ''!r}"
    )

    if fresh is not None and ticket.get("ticket_id"):
        try:
            transcript = fresh.get_context_text(ticket["ticket_id"])
            if transcript:
                user_content += f"\n\n# Freshdesk case content\n{transcript}"
                if fetch_stats is not None:
                    fetch_stats["fetched"] = fetch_stats.get("fetched", 0) + 1
            elif fetch_stats is not None:
                fetch_stats["empty"] = fetch_stats.get("empty", 0) + 1
        except Exception as e:
            if fetch_stats is not None:
                fetch_stats["failed"] = fetch_stats.get("failed", 0) + 1
            print(
                f"[fresh] ticket {ticket.get('ticket_id')}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )

    response = client.chat.completions.create(
        model=_model(),
        max_tokens=400,
        messages=system_messages + [{"role": "user", "content": user_content}],
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "function", "function": {"name": "classify_ticket"}},
    )
    msg = response.choices[0].message
    if not msg.tool_calls:
        raise RuntimeError(f"model returned no tool call (finish_reason={response.choices[0].finish_reason!r})")
    args = msg.tool_calls[0].function.arguments
    data = json.loads(args) if isinstance(args, str) else args
    # gpt-4o-mini occasionally omits `reasoning` (and rarely `confidence`)
    # despite the schema marking both as required. Default rather than crash;
    # the (sub_category, category) pair is the only field that actually drives
    # the output rows — the rest is metadata for the Audit Log.
    sub_cat = data.get("sub_category") or ""
    cat = data.get("category") or ""
    if not sub_cat or not cat:
        raise RuntimeError(f"model returned malformed tool call args: {data!r}")
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return Tier2Result(
        sub_category=sub_cat,
        category=cat,
        confidence=conf,
        reasoning=str(data.get("reasoning") or ""),
    )


def _default_concurrency() -> int:
    if "TIER2_CONCURRENCY" in os.environ:
        return int(os.environ["TIER2_CONCURRENCY"])
    # Default tiers on Azure and OpenAI both have low TPM ceilings (Azure ~10K,
    # OpenAI tier-1 ~200K). Our system prompt is ~6-8K tokens and each call
    # ~3.4K total, so we have to throttle to ~1 req/sec to stay under the
    # OpenAI bucket and avoid the Azure 60-second backoffs. OpenRouter is the
    # only path without per-minute caps under sustained load.
    p = _provider()
    if p == "azure":
        return 2
    if p == "openai":
        return 3
    return 8


DEFAULT_CONCURRENCY = _default_concurrency()


def classify_batch(
    tickets: list[dict],
    on_progress=None,
    concurrency: int = DEFAULT_CONCURRENCY,
    fresh: "fresh_client.FreshClient | None" = None,
    fetch_stats: dict | None = None,
) -> list[Tier2Result | None]:
    """Classify a batch of tickets. Returns None for any that errored.

    on_progress: optional callback (i, total, ticket_id) called after each ticket.
    concurrency: number of in-flight calls (default 8). Override with TIER2_CONCURRENCY.
    fresh: optional Freshdesk client; when supplied each ticket's description +
           transcript is appended to the LLM prompt for context.
    fetch_stats: optional dict mutated in-place with counts of {fetched, empty, failed}.
    """
    if not is_available():
        return [None] * len(tickets)

    client = _make_client()
    # On OpenRouter we mark the system prompt cacheable so Anthropic charges the
    # large stable prefix once per 5-min TTL. OpenAI direct (and Azure OpenAI) do
    # prefix caching automatically (no marker) and reject unknown content fields,
    # so we send a plain string when talking to either.
    if _provider() in ("openai", "azure"):
        system_messages = [{"role": "system", "content": _build_system_prompt()}]
    else:
        system_messages = [{
            "role": "system",
            "content": [{
                "type": "text",
                "text": _build_system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }],
        }]

    # Prime the prompt cache with one synchronous call so the parallel workers
    # below all hit the cache instead of racing to write it.
    results: list[Tier2Result | None] = [None] * len(tickets)
    completed = 0
    if tickets:
        try:
            results[0] = classify_one(client, system_messages, tickets[0], fresh, fetch_stats)
        except Exception as e:
            print(
                f"[tier2] ticket {tickets[0].get('ticket_id')}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
        completed = 1
        if on_progress:
            on_progress(completed, len(tickets), tickets[0].get("ticket_id"))

    if len(tickets) > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            future_to_idx = {
                pool.submit(classify_one, client, system_messages, t, fresh, fetch_stats): i
                for i, t in enumerate(tickets[1:], start=1)
            }
            for fut in as_completed(future_to_idx):
                i = future_to_idx[fut]
                t = tickets[i]
                try:
                    results[i] = fut.result()
                except Exception as e:
                    print(
                        f"[tier2] ticket {t.get('ticket_id')}: {type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                completed += 1
                if on_progress:
                    on_progress(completed, len(tickets), t.get("ticket_id"))

    return results
