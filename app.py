"""Streamlit UI for the AstraZeneca SLA report builder."""
from __future__ import annotations
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

# Streamlit reloads app.py on every interaction, but doesn't always pick up
# edits to modules imported via sys.path.insert. Drop our src/ modules from
# the import cache each run so file changes always take effect.
for _name in ("build_report", "categories", "sla", "tier2_classifier", "fresh_client"):
    sys.modules.pop(_name, None)

import build_report  # noqa: E402


st.set_page_config(page_title="AstraZeneca SLA Report", layout="centered")


# --- Password gate ----------------------------------------------------------
# Only enforced when APP_PASSWORD is set in the environment (e.g. on Render).
# Local runs without the env var skip the gate so dev/test stays one-click.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if APP_PASSWORD:
    if not st.session_state.get("_authed"):
        st.title("AstraZeneca SLA Report")
        pw = st.text_input("Password", type="password")
        if st.button("Sign in", type="primary", use_container_width=True):
            if pw == APP_PASSWORD:
                st.session_state["_authed"] = True
                st.rerun()
            else:
                st.error("Wrong password.")
        st.stop()


st.title("AstraZeneca SLA Report")
st.caption("Upload the Freshdesk export and click Create report. That's it.")

ticket_file = st.file_uploader(
    "Freshdesk ticket export (xlsx or csv)",
    type=["xlsx", "csv"],
)

# Optional country-lookup uploader. The bundled data/email_country.json may be
# absent on a hosted deploy (it contains driver PII so isn't committed). When
# absent, Amy uploads it here and the file is cached in session_state for the
# rest of the browser session.
bundled_lookup_present = (ROOT / "data" / "email_country.json").exists()
with st.expander("Country lookup" + (" (optional override)" if bundled_lookup_present else " (required)"), expanded=not bundled_lookup_present):
    if bundled_lookup_present:
        st.caption("A bundled lookup is loaded. Upload one to override or add markets.")
    else:
        st.caption("Bundled lookup not found — upload the email→country file (xlsx/csv) Jay maintains.")
    lookup_file = st.file_uploader(
        "Country lookup (xlsx or csv)",
        type=["xlsx", "csv"],
        key="lookup_uploader",
    )
    if lookup_file is not None:
        st.session_state["lookup_bytes"] = lookup_file.getvalue()
        st.session_state["lookup_name"] = lookup_file.name
    if st.session_state.get("lookup_name"):
        st.success(f"Cached lookup: **{st.session_state['lookup_name']}**")

create_disabled = ticket_file is None or (not bundled_lookup_present and not st.session_state.get("lookup_name"))

create = st.button(
    "Create report",
    type="primary",
    use_container_width=True,
    disabled=create_disabled,
)

if create:
    workdir = Path(tempfile.mkdtemp(prefix="astra_sla_"))

    in_path = workdir / ticket_file.name
    in_path.write_bytes(ticket_file.getvalue())

    extra_country_files: list[Path] = []
    if st.session_state.get("lookup_bytes"):
        lookup_path = workdir / st.session_state["lookup_name"]
        lookup_path.write_bytes(st.session_state["lookup_bytes"])
        extra_country_files.append(lookup_path)

    with st.status("Building report…", expanded=True) as status_box:
        st.write(f"Input: `{in_path.name}`")
        month = build_report.detect_month(in_path)
        if not month:
            today = date.today()
            month = f"{today.year}-{today.month:02d}"
            st.write(f"Could not auto-detect month from data — falling back to **{month}**.")
        else:
            st.write(f"Detected reporting month: **{month}**")

        out_path = workdir / f"AstraZeneca_SLA_Report_{month}.xlsx"

        progress = st.progress(0.0)
        ticker = st.empty()

        def progress_cb(stage: str, cur: int, total: int, detail: str):
            labels = {"read": "Reading export", "tier2": "Categorising with LLM", "write": "Writing file"}
            label = labels.get(stage, stage)
            if total:
                progress.progress(min(cur / total, 1.0))
            ticker.write(f"{label} — {cur}/{total} {detail}")

        try:
            stats = build_report.build(
                in_path,
                month,
                out_path,
                extra_country_files=extra_country_files or None,
                progress_cb=progress_cb,
            )
        except Exception as e:
            status_box.update(label="Build failed", state="error")
            st.exception(e)
            st.stop()

        progress.progress(1.0)
        ticker.empty()
        status_box.update(label=f"Done — {stats['out_rows']} rows", state="complete")

        st.write(
            f"**Tier-1 rules:** {stats['tier1_classified']}  ·  "
            f"**Tier-2 LLM:** {stats['tier2_classified']}  ·  "
            f"**Skipped (wrong month):** {stats['skipped_wrong_month']}"
        )
        if stats.get("unknown_emails"):
            st.warning(
                f"{len(stats['unknown_emails'])} email(s) had no country mapping — reported as 'Other'."
            )

    st.download_button(
        label=f"Download {out_path.name}",
        data=out_path.read_bytes(),
        file_name=out_path.name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
