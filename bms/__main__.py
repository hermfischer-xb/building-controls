"""Entrypoint.

    .venv/bin/python -m bms --config config/devices.yaml
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .api import create_app
from .config import load


def main() -> None:
    parser = argparse.ArgumentParser(prog="bms", description=__doc__)
    parser.add_argument("--config", default="config/devices.yaml")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = load(args.config)
    app = create_app(cfg)

    if cfg.api_host == "0.0.0.0":  # noqa: S104 - the point is to refuse it
        raise SystemExit(
            "refusing to bind 0.0.0.0: this host has a public interface. "
            "Bind 127.0.0.1 and reach it over the VPN."
        )

    uvicorn.run(
        app,
        host=cfg.api_host,
        port=cfg.api_port,
        log_level=args.log_level,
        # Parse X-Forwarded-* only when a proxy is declared, and only from the
        # loopback address it connects from. Left on by default, uvicorn would
        # believe those headers from anyone that reached it directly.
        proxy_headers=cfg.behind_proxy,
        forwarded_allow_ips="127.0.0.1" if cfg.behind_proxy else None,
    )


if __name__ == "__main__":
    main()
