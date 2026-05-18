#!/usr/bin/env bash
# Render start command. Set the Render service's "Start Command" to:
#     bash start.sh
# Render injects $PORT; bind to 0.0.0.0 so the service is reachable.
set -e
exec streamlit run app.py \
  --server.port "${PORT:-8501}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false
