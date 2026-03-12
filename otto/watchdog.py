"""
otto.watchdog -- Background permission watchdog for Antigravity.

Monitors the state DB for permission regressions and auto-patches them.
Also cleans notification bloat when it exceeds a threshold.

Usage:
    python -m otto.watchdog              # Run in foreground (Ctrl+C to stop)
    python -m otto.watchdog --interval 30  # Check every 30 seconds
    python -m otto.watchdog --once       # Single check, then exit
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

from .core import (
    BOOL_SENTINEL_KEYS,
    OPTIMAL_AGENT_PREFS_B64,
    SENTINEL_KEYS,
    decode_policy_value,
    get_key,
    open_db,
    paths,
    set_key,
)

NOTIFICATION_THRESHOLD = 50
WATCHDOG_LOG = Path(__file__).parent / "watchdog.log"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("otto-watchdog")
    logger.setLevel(logging.INFO)

    fh = logging.FileHandler(str(WATCHDOG_LOG), encoding="utf-8")
    fh.setLevel(logging.INFO)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def check_and_fix_permissions(logger: logging.Logger) -> bool:
    conn = open_db(paths.global_state_db)
    if conn is None:
        logger.debug("State DB not accessible")
        return False

    fixed = False
    try:
        raw = get_key(conn, "antigravityUnifiedStateSync.agentPreferences")
        if raw is None:
            logger.warning("agentPreferences key missing -- writing optimal defaults")
            set_key(conn, "antigravityUnifiedStateSync.agentPreferences", OPTIMAL_AGENT_PREFS_B64)
            return True

        try:
            proto_bytes = base64.b64decode(raw)
        except Exception:
            logger.error("agentPreferences is not valid base64 -- overwriting")
            set_key(conn, "antigravityUnifiedStateSync.agentPreferences", OPTIMAL_AGENT_PREFS_B64)
            return True

        all_ok = True
        for sentinel_key, (expected_val, label) in SENTINEL_KEYS.items():
            if sentinel_key not in proto_bytes:
                logger.warning(f"{label} sentinel key missing")
                all_ok = False
                continue
            idx = proto_bytes.index(sentinel_key) + len(sentinel_key)
            remaining = proto_bytes[idx:]
            b64_start = remaining.find(b"\x12\x06\n\x04")
            if b64_start >= 0:
                b64_val = remaining[b64_start + 4 : b64_start + 8].decode("ascii", errors="replace")
                policy_val = decode_policy_value(b64_val)
                if policy_val != expected_val:
                    logger.warning(f"{label} regressed: value={policy_val}, expected={expected_val}")
                    all_ok = False

        for bool_key, label in BOOL_SENTINEL_KEYS.items():
            if bool_key not in proto_bytes:
                logger.warning(f"{label} missing from agentPreferences")
                all_ok = False

        if not all_ok:
            logger.info("Patching agentPreferences back to optimal values")
            set_key(conn, "antigravityUnifiedStateSync.agentPreferences", OPTIMAL_AGENT_PREFS_B64)
            fixed = True
        else:
            logger.debug("Permissions OK")

    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            logger.debug("State DB locked -- will retry next cycle")
        else:
            logger.error(f"DB error: {e}")
    finally:
        conn.close()

    return fixed


def check_and_clean_notifications(logger: logging.Logger) -> bool:
    conn = open_db(paths.global_state_db)
    if conn is None:
        return False

    cleaned = False
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM ItemTable WHERE key LIKE 'antigravity.notification%'"
        ).fetchone()[0]

        if count > NOTIFICATION_THRESHOLD:
            logger.info(f"Notification bloat: {count} entries (threshold={NOTIFICATION_THRESHOLD})")
            conn.execute("DELETE FROM ItemTable WHERE key LIKE 'antigravity.notification%'")
            conn.commit()
            logger.info(f"Cleaned {count} notification entries")
            cleaned = True
    except sqlite3.OperationalError as e:
        if "locked" not in str(e):
            logger.error(f"DB error: {e}")
    finally:
        conn.close()

    return cleaned


def trim_log_file(logger: logging.Logger, max_size_kb: int = 512) -> None:
    if not WATCHDOG_LOG.exists():
        return
    if WATCHDOG_LOG.stat().st_size / 1024 > max_size_kb:
        try:
            lines = WATCHDOG_LOG.read_text(encoding="utf-8").splitlines(keepends=True)
            WATCHDOG_LOG.write_text("".join(lines[-200:]), encoding="utf-8")
            logger.info("Trimmed watchdog.log to last 200 lines")
        except OSError:
            pass


_running = True


def _signal_handler(signum, frame):
    global _running
    _running = False


def run_once(logger: logging.Logger) -> dict:
    actions = {}
    if check_and_fix_permissions(logger):
        actions["permissions_patched"] = True
    if check_and_clean_notifications(logger):
        actions["notifications_cleaned"] = True
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="OTTO permission watchdog")
    parser.add_argument("--interval", type=int, default=60, help="Check interval (seconds)")
    parser.add_argument("--once", action="store_true", help="Single check, then exit")
    args = parser.parse_args()

    logger = setup_logging()
    logger.info(f"Watchdog started (PID={os.getpid()}, interval={args.interval}s)")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if args.once:
        actions = run_once(logger)
        logger.info(f"Single check complete -- actions: {actions or 'none'}")
        return 0

    cycle = 0
    global _running
    while _running:
        cycle += 1
        try:
            actions = run_once(logger)
            if actions:
                logger.info(f"Cycle {cycle}: {actions}")
            if cycle % 100 == 0:
                trim_log_file(logger)
        except Exception as e:
            logger.error(f"Cycle {cycle} error: {e}")

        for _ in range(args.interval):
            if not _running:
                break
            time.sleep(1)

    logger.info("Watchdog stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
