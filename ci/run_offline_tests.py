"""Run Linux persistence regressions with all external networking blocked."""
import argparse
import os
from pathlib import Path
import platform
import socket
import sys
import unittest
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, default=Path.cwd())
parser.add_argument(
    "modules",
    nargs="*",
    default=["test_bot_once", "test_api_resilience", "test_one_share", "test_swing_strategy", "test_hourly_runtime"],
)
args = parser.parse_args()
source = args.source.resolve()
os.chdir(source)
sys.path.insert(0, str(source))
os.environ.update(
    DRY_RUN="Y",
    ENABLE_LIVE_ORDERS="NO",
    FORCE_BAA="N",
    GITHUB_STATE_MODE="off",
    KIS_APP_KEY="offline-fake-key",
    KIS_APP_SECRET="offline-fake-secret",
    KIS_CANO="12345678",
    TELEGRAM_BOT_TOKEN="",
    TELEGRAM_CHAT_ID="",
)
print(f"Platform: {platform.platform()}; Python: {sys.version}", flush=True)
if sys.platform != "linux":
    raise SystemExit("This runner requires real Linux; no fcntl/fsync substitutes.")
print(Path("/etc/os-release").read_text(encoding="utf-8"), flush=True)

with (
    patch.object(
        socket.socket,
        "connect",
        side_effect=AssertionError("External network blocked in offline tests"),
    ),
    patch.object(
        socket.socket,
        "connect_ex",
        side_effect=AssertionError("External network blocked in offline tests"),
    ),
):
    suite = unittest.defaultTestLoader.loadTestsFromNames(args.modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)

