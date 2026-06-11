"""Run the harness backend:  python -m backend   (from the delegate_mcp dir)."""

import os
import uvicorn


def main():
    uvicorn.run(
        "backend.app:app",
        host=os.environ.get("HARNESS_HOST", "127.0.0.1"),
        port=int(os.environ.get("HARNESS_PORT", "8787")),
        reload=bool(os.environ.get("HARNESS_RELOAD")),
    )


if __name__ == "__main__":
    main()
