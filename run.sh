#!/usr/bin/env bash
# Convenience launcher for the Streamlit app.
set -e
cd "$(dirname "$0")"

# Optional: load .env if present (for OPENROUTER_API_KEY)
if [ -f .env ]; then
  set -a; source .env; set +a
fi

python3 -m streamlit run app.py
