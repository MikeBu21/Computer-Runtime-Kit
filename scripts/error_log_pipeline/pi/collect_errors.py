#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Periodenende: erfassen und, falls etwas zu melden ist, ein Paket in die Outbox legen.

Die eigentliche Erfassung (App-Logs, Systemjournal, Systemzustand) steckt in fault_scan.py.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fault_scan  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=os.environ.get(
            "ERROR_LOG_PIPELINE_CONFIG",
            str(Path(__file__).resolve().parent / "config.json"),
        ),
    )
    args = ap.parse_args()
    return fault_scan.main(["--config", args.config, "--collect"])


if __name__ == "__main__":
    raise SystemExit(main())
