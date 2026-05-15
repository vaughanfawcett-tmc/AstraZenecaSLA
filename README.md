# AstraZeneca SLA Report Builder

Drop the Freshdesk ticket export in, click build, get the finished AstraZeneca SLA report out.

Replaces the manual monthly process documented in `Astra Zeneca Quarterly Stat Instructions.docx`.

## Quick start

```bash
# 1. Install
python3 -m pip install -r requirements.txt

# 2. (Optional) Set OpenRouter key for AI categorisation
export OPENROUTER_API_KEY=sk-or-v1-...

# 3. Run the app
./run.sh
# or:  python3 -m streamlit run app.py
```

The app opens in your browser. Drop the ticket export, pick the reporting month, click Build, download the result.

## CLI mode

```bash
cd src
python3 build_report.py "/path/to/Ticket Export.xlsx" 2026-01
# Output → ../output/AstraZeneca_SLA_Report_2026-01.xlsx
```

## What it does

1. **Filter** the export to AstraZeneca tickets (Contact ID / Full name contains `astrazeneca`)
2. **Reshape** into the SLA report layout
3. **Country lookup** — `data/email_country.json` (5,477 emails) merged from prior reports and Jay's official driver roster. AstraZeneca uses a closed list of 25 valid markets; anything not on the list (or unmapped) is reported as `Other`.
4. **SLA verdict** — Within if all three Freshdesk status columns are within SLA; Outside if any is violated; blank if the ticket is too new (< 48 working hours) to judge
5. **Categorise** in two tiers:
   - Tier 1: deterministic rules + subject patterns (~60% of rows, 92% accuracy)
   - Tier 2: Claude Haiku 4.5 with a cached prompt + closed category list (the remaining ~40%)
6. **Quality Failures tab** — per-country pivot of within / outside SLA
7. **Audit Log tab** — per-row trail showing which tier classified each row, the confidence, and the reason

## Output tabs

| Tab | Contents |
|---|---|
| Data | The actual SLA report — same shape Amy currently sends |
| Categories | The closed list of valid (Sub Category, Category) pairs |
| Quality Failures | Country × within/outside/% pivot |
| Audit Log | Per-row decision trail |

Yellow-highlighted rows in the Data tab are Tier-2 classifications below 60% confidence — worth eyeballing before sending out.

## Project structure

```
.
├── app.py                  Streamlit UI
├── run.sh                  Convenience launcher
├── requirements.txt
├── SOLUTION.md             Design doc — kept current as decisions land
├── src/
│   ├── build_report.py     Pipeline entry point (also CLI)
│   ├── categories.py       Closed list + Tier-1 rules
│   ├── sla.py              SLA verdict logic
│   └── tier2_classifier.py Tier-2 LLM classifier (Claude Haiku 4.5)
├── data/
│   └── email_country.json  Email→country map + 25-market allow-list (5,477 emails)
└── output/                 Generated reports land here in CLI mode
```

## Configuration

| Env var              | Purpose                                                                    |
|----------------------|----------------------------------------------------------------------------|
| `OPENROUTER_API_KEY`  | Enables Tier-2 LLM classification. Without it, ~40% of rows have blank categories. |

Drop a `.env` file in the project root with `OPENROUTER_API_KEY=...` and `run.sh` will pick it up.

## Cost ballpark

Tier-2 uses Claude Haiku 4.5 (cheapest model) with prompt caching on the system prompt (which contains the closed category list and decision rules). Expected monthly cost for ~270 Tier-2 calls per AstraZeneca run: well under £1.

## Open work (Phase 2)

- **Freshdesk API access** — once we have a key with read scope on conversations, Tier 2 can pull the actual email/chat body for ambiguous tickets, which should push categorisation accuracy from ~92% to >97%.
- **Quality Failures tab format** — currently a sensible default (Country / Total / Within / Outside / % Outside with totals). Will tweak to match Amy's expected layout once she shares a sample.
