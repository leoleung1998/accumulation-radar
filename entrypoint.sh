#!/bin/bash
set -e
cron
exec uvicorn dashboard:app --host 0.0.0.0 --port 8000
