"""Run the harness backend:  python -m backend   (from the delegate_mcp dir)."""

import os
import pathlib

import uvicorn

try:  # honor the documented `cp .env.example .env` setup
    from dotenv import load_dotenv

    load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


def main():
    uvicorn.run(
        "backend.app:app",
        host=os.environ.get("HARNESS_HOST", "127.0.0.1"),
        port=int(os.environ.get("HARNESS_PORT", "8787")),
        reload=bool(os.environ.get("HARNESS_RELOAD")),
    )


if __name__ == "__main__":
    main()
