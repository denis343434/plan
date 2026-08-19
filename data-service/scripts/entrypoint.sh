#!/usr/bin/env sh
set -e

python - <<'PY'
import sys
import time

import psycopg

from app.config import settings

dsn = settings.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")

for attempt in range(30):
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            print("database is ready")
            sys.exit(0)
    except psycopg.OperationalError:
        print(f"waiting for database... ({attempt + 1}/30)")
        time.sleep(1)

print("database not available, giving up")
sys.exit(1)
PY

alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
