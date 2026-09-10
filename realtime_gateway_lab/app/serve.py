# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn[standard]",
#   "sqlalchemy[asyncio]>=2.0",
#   "asyncpg",
#   "redis",
# ]
# ///
"""Host-mode entry point. `./lab.sh app` runs this; the containers use uvicorn
directly. Both import the same `app.main:app`."""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("APP_PORT", "8093")),
        log_level="info",
    )
