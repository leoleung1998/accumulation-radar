#!/bin/bash
set -e

echo "🏦 Running initial pool scan on startup..."
/usr/local/bin/python3 /app/accumulation_radar.py pool
echo "✅ Pool scan complete"

exec uvicorn dashboard:app --host 0.0.0.0 --port 8000
