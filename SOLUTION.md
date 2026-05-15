# AstraZeneca SLA Report — Automation Solution

Working notes from analysing the three source files Amy shared. Numbers below come from running rule prototypes against the January 2026 export and the matching SLA report.

## What Amy does today (the manual baseline)

Once a month (by the 5th):

1. Export every Freshdesk ticket for the month (~7,400 rows across all clients).
2. Filter down to AstraZeneca tickets (~679 rows in Jan).
3. Re-shape into the SLA report layout: `Month, Subcase Title, Sub Category, Category, Case Number, Customer, Email Address, Country, Within, Outside`.
4. **Classify every ticket** into one of ~30 client-defined `(Sub Category, Category)` pairs. For unclear ones she opens the ticket in Freshdesk and reads the conversation.
5. **Country lookup** — XLOOKUP from email address against a list Jay sends.
6. **SLA verdict** — Within = all of `Resolution Status / First Response Status / Every Response Status` are `Within SLA`; Outside = any is `SLA Violated`.
7. **Quality failures tab** — per-country count and % of Outside-SLA emails.
8. Send to Jay quarterly.

The categorisation step is what takes the time. Everything else is mechanical.

## What's automatable (and how confident)

| Step | Approach | Confidence |
|---|---|---|
| Filter to Astra tickets | `Contact ID` ends with `@astrazeneca.com` | Trivial. Note: Amy's instructions say "search 'Astra Zeneca'", but the actual filter is the email domain. |
| Column re-mapping | Direct field copy | Trivial |
| Country lookup | Email → country dictionary (need Jay's file) | Trivial once we have the file |
| Within / Outside SLA | Bool over the three status columns | Trivial — verified against Jan data, all 6 Outside rows had `First response status = SLA Violated` |
| Quality-failures tab | Pivot by country, count Outside / count Total | Trivial |
| **Categorisation** | Hybrid — see below | The interesting bit |

## Categorisation strategy — a 3-tier funnel

Tested against all 676 January rows (Amy's calls = ground truth):

### Tier 1 — Deterministic rules from Freshdesk's own fields

The export already carries `Query Regarding` + `Type` fields. For some pairs the mapping to the client's categories is effectively 1:1:

| Freshdesk (Query Regarding, Type) | → Client (Sub Category, Category) | Vol | Hit rate |
|---|---|---|---|
| `Updates / New Account` | `Account Details / Fleet Logistics Update` | 64 | 100% |
| `Updates / Mobile Number` | `Account Details / Fleet Logistics Update` | 3 | 100% |
| `Audit / Velocity` | `Audit / Multi Fuel` | 19 | 100% |
| `Audit Response / Missing Fuel` | `Audit / Missing Fuel` | 22 | 96% |
| `Audit Response / Client Audit Dashboard` | `Audit / High Mileage (Total)` | 5 | 100% |
| `Updates / Line Manager` | `Account Details / Fleet Logistics Update` | 77 | 96% |
| `Updates / Leaver` | `Account Details / Fleet Logistics Update` | 25 | 88% |
| `Missed Cut-Off / Pushed Back` | `Mileage Entry / Employee Sent Mileage To TMC` | 122 | 86% |
| `Closing Period / After Month Close` | `Mileage Entry / Employee Sent Mileage To TMC` | 37 | 81% |

Plus subject-line patterns:

- `[<digits>]Astra Zeneca: <New|Leaver|Vehicle|Mobile>` → Fleet Logistics Update
- `[<digits>]Amendment to be made` → Fleet Logistics Update
- Regex catch-all `^\[\d+\]\[\d+\]` → Fleet Logistics Update

**Result on Jan data: 412 / 676 rows (61%) classified by rules, 92% agreement with Amy's manual call.** The 8% rule-disagreements are mostly edge cases where Amy clearly read the email body (e.g. a `Missed Cut-Off / Pushed Back` ticket whose subject is "Compte bloqué" — actually a password reset).

### Tier 2 — LLM classification with conversation body

For the remaining ~39% (264 of 676) — and for the ambiguous Tier-1 cases — pull the email/chat body from Freshdesk via API and ask Claude to pick from the closed list of valid `(Sub Category, Category)` pairs. Returns:
- chosen pair
- confidence score
- one-line reasoning (audit trail)

**Requires:** Freshdesk API key + permission to read ticket conversations.

If we don't have API access, Tier 2 can fall back to subject-only LLM classification — quality drops on ticket subjects like "Conversation with User" but still beats nothing.

### Tier 3 — Human review queue

Anything below a confidence threshold goes into a `Review` sheet of the output. Amy eyeballs ~30-50 rows instead of 676. Her overrides feed back as new rules.

## Proposed architecture

A small Python tool (CLI or simple drag-and-drop UI):

```
inputs/
  ticket_export.xlsx         (raw Freshdesk export, multi-client)
  email_country_map.xlsx     (Jay's lookup file — TBC)
  reporting_month.txt        (e.g. "2026-01")

         │
         ▼
  ┌─────────────────────┐
  │  1. Filter & shape  │  pandas — drop non-Astra rows, rename cols
  └──────────┬──────────┘
             ▼
  ┌─────────────────────┐
  │  2. Country lookup  │  email → country dict
  └──────────┬──────────┘
             ▼
  ┌─────────────────────┐
  │  3. SLA verdict     │  bool over 3 status cols
  └──────────┬──────────┘
             ▼
  ┌─────────────────────┐
  │  4. Categorise      │  Tier 1 → Tier 2 (Freshdesk API + Claude) → Tier 3
  └──────────┬──────────┘
             ▼
  ┌─────────────────────┐
  │  5. Build workbook  │  openpyxl — Data tab, Categories tab, Quality Failures tab
  └─────────────────────┘
         │
         ▼
outputs/AstraZeneca_SLA_Report_<month>.xlsx
log.csv  (per-row decision + tier + confidence + reasoning — for audit)
```

## Rollout plan

1. **Build Tier 1 + the mechanical bits** — filter, reshape, country lookup, SLA verdict, quality-failures pivot. Outputs a fully-formed report with category gaps for the ~40% of tickets that need a body. Days, not weeks.
2. **Add Tier 2 — Freshdesk API + Claude classification.** This is where most of the time saving lands.
3. **Demo with a few months of historical data**, compare to Amy's calls, tune the rule book and prompts. As discussed on the call, the first few runs are co-piloted; logic gets refined from disagreements.
4. **Optional later: fully automated trigger** (cron on the 1st of each month, drops file in OneDrive/Teams, pings Amy).

Phase-2 work mirrors the HSBC SLA automation Harrison flagged — the SLA verdict + per-country quality pivot is essentially identical to that one.

## Decisions from Amy (2026-05-05)

- **Country lookup file** — Amy is forwarding it. ⏳ awaiting receipt.
- **Quality Failures tab** — Amy is forwarding a sample. ⏳ awaiting receipt.
- **Three "missing" Jan tickets** — they were skipped because the SLA due-date hadn't yet passed at month-end. **Rule:** include every Astra ticket; if a ticket is < 48 working hours old at run time and SLA can't yet be determined, leave Within/Outside blank — Amy will manually add a note where needed.
- **Category list duplicates** — typos. Collapse `Audit ` → `Audit` and `Payroll ` → `Payroll` (and any other trailing-space variants). Treat as a single category each.
- **Delivery format** — drop-the-file-in, get the finished xlsx out. So: CLI / simple desktop tool. No web app, no scheduler for now.

## Still open (for Phase 2)

- **Freshdesk API access** — Tier 2 categorisation needs a key with read scope on ticket conversations. Will request once Phase 1 lands.

## File pointers (verified 2026-04-30)

- Raw export: `~/Downloads/Ticket Export - January 2026.xlsx` — 7,401 rows × 41 cols, all clients
- Target output: `~/Downloads/AstraZeneca SLA Report Q1 2026.xlsx` — 676 rows (Jan only so far)
- Instructions: `~/Downloads/Astra Zeneca Quarterly Stat Instructions.docx`
- Long-term home: Teams → Service → AZ Updates folder
