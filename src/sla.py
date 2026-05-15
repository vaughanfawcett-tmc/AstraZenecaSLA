"""SLA verdict logic.

Within = none of the three status fields are 'SLA Violated'
Outside = any is 'SLA Violated'
Blank = ticket too new to judge (less than 48 working hours since creation)
"""
from __future__ import annotations
from datetime import datetime, time, timedelta


def _norm(v) -> str:
    return (str(v) if v is not None else "").strip()


def working_hours_between(start: datetime, end: datetime) -> float:
    """Hours between two datetimes counting only Mon–Fri 09:00–17:00 local time.

    A simplification: ignores public holidays. 8 working hours per day.
    """
    if end <= start:
        return 0.0
    work_start = time(9, 0)
    work_end = time(17, 0)
    hours = 0.0
    cur = start
    while cur < end:
        # If weekend, skip to next Monday 09:00
        if cur.weekday() >= 5:
            days_to_mon = 7 - cur.weekday()
            cur = datetime.combine((cur + timedelta(days=days_to_mon)).date(), work_start)
            continue
        # If before working hours, jump to 09:00
        if cur.time() < work_start:
            cur = datetime.combine(cur.date(), work_start)
            continue
        # If after working hours, jump to next day 09:00
        if cur.time() >= work_end:
            cur = datetime.combine((cur + timedelta(days=1)).date(), work_start)
            continue
        # End-of-day for current day
        eod = datetime.combine(cur.date(), work_end)
        chunk_end = min(end, eod)
        hours += (chunk_end - cur).total_seconds() / 3600.0
        cur = chunk_end
        if cur >= eod:
            cur = datetime.combine((cur + timedelta(days=1)).date(), work_start)
    return hours


def sla_verdict(
    resolution_status: str | None,
    first_response_status: str | None,
    every_response_status: str | None,
    created_time: datetime | None,
    now: datetime | None = None,
    too_new_threshold_hours: float = 48.0,
    tags: str | None = None,
) -> tuple[str, str]:
    """Return (within, outside) — each 'Y' or '' — Amy's report layout uses two cols.

    If any status is 'SLA Violated' → Outside, UNLESS the breach is on Resolution
    only and the ticket was auto-closed (tags contain 'AutoClosed'). Per Amy's
    Jan 2026 ground truth, AutoClosed tickets where first/every response stayed
    within SLA are counted as Within — the customer went silent, not TMC.
    """
    statuses = [_norm(resolution_status), _norm(first_response_status), _norm(every_response_status)]
    tag_str = _norm(tags).lower()

    res_violated = statuses[0].lower() == "sla violated"
    fr_violated  = statuses[1].lower() == "sla violated"
    er_violated  = statuses[2].lower() == "sla violated"

    if res_violated and not (fr_violated or er_violated) and "autoclosed" in tag_str:
        return ("Y", None)

    if res_violated or fr_violated or er_violated:
        return (None, "Y")

    if any(s.lower() == "within sla" for s in statuses):
        return ("Y", None)

    # All blank — check age. Too-new tickets leave both blank for Amy to annotate.
    if created_time and now:
        age = working_hours_between(created_time, now)
        if age < too_new_threshold_hours:
            return (None, None)

    # Default: assume within (Freshdesk leaves blank when nothing's overdue and resolution closed within window)
    return ("Y", None)
