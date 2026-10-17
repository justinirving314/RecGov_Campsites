"""Run the server: ``uv run campwatch`` (or ``python -m campwatch``)."""

from __future__ import annotations

import argparse
import logging

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CampWatch server.")
    parser.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other devices")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run("campwatch.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
