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
    "Freshdesk ticket export (xlsx, xls, or csv)",
)

# Clear cached report when a different file is uploaded
uploaded_name = ticket_file.name if ticket_file else None
if uploaded_name != st.session_state.get("_last_upload"):
    st.session_state.pop("_report_bytes", None)
    st.session_state.pop("_report_name", None)
    st.session_state.pop("_report_stats", None)
    st.session_state["_last_upload"] = uploaded_name

create = st.button(
    "Create report",
    type="primary",
    use_container_width=True,
    disabled=ticket_file is None,
)

if create:
    workdir = Path(tempfile.mkdtemp(prefix="astra_sla_"))

    in_path = workdir / ticket_file.name
    in_path.write_bytes(ticket_file.getvalue())

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

    # Store report in session state so it survives the rerun triggered by
    # clicking the download button.
    st.session_state["_report_bytes"] = out_path.read_bytes()
    st.session_state["_report_name"] = out_path.name
    st.session_state["_report_stats"] = stats

# Show download button whenever a completed report is in session state.
if "_report_bytes" in st.session_state:
    st.download_button(
        label=f"Download {st.session_state['_report_name']}",
        data=st.session_state["_report_bytes"],
        file_name=st.session_state["_report_name"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
