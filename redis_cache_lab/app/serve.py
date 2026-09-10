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
"""Entry point. `./lab.sh app` runs this; uv fetches the dependencies itself."""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("APP_PORT", "8001")),
        reload=False,
        log_level="info",
    )
