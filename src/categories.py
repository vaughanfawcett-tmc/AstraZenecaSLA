"""Closed list of valid (Sub Category, Category) pairs and Tier-1 deterministic rules."""
from __future__ import annotations
import re

# Canonical category list (deduped — trailing-space variants collapsed per Amy 2026-05-05)
CATEGORIES: list[tuple[str, str]] = [
    ("Account Access", "Password Reset"),
    ("Account Details", "Employee Vehicle Change Request"),
    ("Account Details", "Line Manager Reporting"),
    ("Account Details", "ID Update"),
    ("Account Details", "Change of countries"),
    ("Account Details", "Scheme Change"),
    ("Account Details", "Fleet Logistics Update"),
    ("Account Details", "AZ Update"),
    ("Account Details", "Account Check"),
    ("App", "EV Tariff informantion"),  # client's spelling — preserved
    ("Audit", "High Mileage (Total)"),
    ("Audit", "High Mileage (Daily)"),
    ("Audit", "High Purchasing Costs (High PPL)"),
    ("Audit", "Driver Entered Cost"),
    ("Audit", "Multi Fuel"),
    ("Audit", "2 or more Multiple Fills with small Transaction"),
    ("Audit", "Missing Fuel"),
    ("Audit", "Small Fills"),
    ("Audit", "Mileage Variance"),
    ("Audit", "Rounding of Mileage"),
    ("Audit", "High Public Charging"),
    ("Auto Close Reminder", "Odometer Reading Correction"),
    ("Auto Close Reminder", "Employee Sent Mileage To TMC"),
    ("Card Query", "Citi bank card query"),
    ("Invoice", "Invoice"),
    ("Mileage Entry", "Odometer Reading Correction"),
    ("Mileage Entry", "Employee Sent Mileage To TMC"),
    ("Payroll", "Payroll Query"),
]


def _norm(v: str | None) -> str:
    return (v or "").strip()


# Tier-1 rules: (Freshdesk Query Regarding, Type) → (Sub Category, Category)
# Confidence is the empirical hit rate observed against Amy's Jan 2026 ground truth.
# Rules at ≥0.90 confidence are trusted outright (see TIER1_TRUST_THRESHOLD in
# build_report.py); below that, the answer is captured but the row is also
# routed to Tier-2 for LLM verification.
RULES: dict[tuple[str, str | None], tuple[tuple[str, str], float]] = {
    ("Updates", "New Account"):                      (("Account Details", "Fleet Logistics Update"),  1.00),
    ("Updates", "Mobile Number"):                    (("Account Details", "Fleet Logistics Update"),  1.00),
    ("Updates", "Line Manager"):                     (("Account Details", "Fleet Logistics Update"),  0.96),
    ("Updates", "Leaver"):                           (("Account Details", "Fleet Logistics Update"),  0.88),
    ("Audit", "Velocity"):                           (("Audit", "Multi Fuel"),                        1.00),
    ("Audit Response", "Velocity"):                  (("Audit", "Multi Fuel"),                        1.00),
    ("Audit Response", "Missing Fuel"):              (("Audit", "Missing Fuel"),                      0.96),
    ("Audit Response", "Client Audit Dashboard"):    (("Audit", "High Mileage (Total)"),              1.00),
    ("Audit Response", "Welcome Calls"):             (("Account Access", "Password Reset"),           1.00),
    # 'Closing Period' / 'After Month Close' — bumped above the trust threshold
    # against Jan 2026: when Freshdesk marked the ticket this way, Amy's manual
    # report assigned Mileage Entry/ESMTT for the clear majority (the Tier-2 LLM
    # was over-flipping these to Auto Close Reminder).
    ("Closing Period", "After Month Close"):         (("Mileage Entry", "Employee Sent Mileage To TMC"), 0.91),
    # Same story for 'Missed Cut-Off' / 'Pushed Back' — Amy treats this as a
    # Mileage Entry, not the literal Auto Close Reminder bucket.
    ("Missed Cut-Off", "Pushed Back"):               (("Mileage Entry", "Employee Sent Mileage To TMC"), 0.93),
    ("Missed Cut-Off", "Reopened"):                  (("Mileage Entry", "Employee Sent Mileage To TMC"), 1.00),
    ("Account Details", "Report Request"):           (("Account Details", "Fleet Logistics Update"),  1.00),
    # NOTE — two combos that looked like clean rules from the disagreement
    # dump turned out to be too mixed once we checked Amy's full distribution
    # (`scripts/amy_ground_truth.py`). Leaving them as Tier-2-only:
    #   ('Account Details', 'Resent Logon Details') n=47: 60% Password Reset,
    #     19% Account Check, 13% Mileage Entry — the LLM gets to read the
    #     subject and pick.
    #   ('Technical Issue', 'App Support') n=38: 34% Account Check, 26%
    #     Mileage Entry, 18% Password Reset, 13% Auto Close Reminder — no
    #     dominant label.
    ("EV", "Tariff Query"):                          (("App", "EV Tariff informantion"),              0.67),
    ("Payroll", "Explanation of Payroll Calculation"): (("Payroll", "Payroll Query"),                 1.00),
    ("Invoicing", None):                             (("Account Details", "Fleet Logistics Update"),  1.00),
}

# Subject regex patterns — when matched, override field-based rules
SUBJECT_PATTERNS: list[tuple[re.Pattern, tuple[str, str], float]] = [
    (re.compile(r"^\s*\[\d+\].*Astra Zeneca:", re.I),       ("Account Details", "Fleet Logistics Update"), 0.99),
    (re.compile(r"^\s*\[\d+\].*Amendment to be made", re.I), ("Account Details", "Fleet Logistics Update"), 0.99),
    (re.compile(r"^\s*\[\d+\]\[\d+\]", re.I),               ("Account Details", "Fleet Logistics Update"), 0.99),
]


def classify_tier1(query_regarding: str | None, type_: str | None, subject: str | None) -> tuple[tuple[str, str], float, str] | None:
    """Return ((sub_category, category), confidence, reason) or None if no rule fires."""
    subj = _norm(subject)
    qr = _norm(query_regarding)
    typ = _norm(type_)

    # Subject patterns take priority — they're the most specific signal
    for pat, target, conf in SUBJECT_PATTERNS:
        if pat.search(subj):
            return target, conf, f"subject pattern: {pat.pattern}"

    # Then field-based rules
    if (qr, typ) in RULES:
        target, conf = RULES[(qr, typ)]
        return target, conf, f"rule: ({qr!r}, {typ!r})"
    if (qr, None) in RULES:
        target, conf = RULES[(qr, None)]
        return target, conf, f"rule: ({qr!r}, *)"

    return None
