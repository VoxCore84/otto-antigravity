"""
otto.optimizer -- Antigravity performance optimizer.

Checks permissions, notification bloat, DB sizes, log dirs, extension
registry integrity, and settings.json against the baseline.

Usage:
    python -m otto.optimizer              # Full scan
    python -m otto.optimizer --quick      # Skip vacuums/log cleanup
    python -m otto.optimizer --fix        # Auto-patch what we can
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import sqlite3
import sys

from .core import (
    BOOL_SENTINEL_KEYS,
    OPTIMAL_AGENT_PREFS_B64,
    SENTINEL_KEYS,
    CheckResult,
    Report,
    db_size_kb,
    decode_policy_value,
    get_key,
    load_baseline,
    open_db,
    paths,
    set_key,
)


# ---------------------------------------------------------------------------
# Check: Permission policies
# ---------------------------------------------------------------------------
def check_permissions(report: Report, fix: bool = False) -> None:
    conn = open_db(paths.global_state_db)
    if conn is None:
        report.add("Permissions", "ERROR", f"State DB not found: {paths.global_state_db}")
        return

    try:
        raw = get_key(conn, "antigravityUnifiedStateSync.agentPreferences")
        if raw is None:
            report.add("Permissions", "WARN", "agentPreferences key not found")
            if fix:
                set_key(conn, "antigravityUnifiedStateSync.agentPreferences", OPTIMAL_AGENT_PREFS_B64)
                report.add("Permissions (fix)", "FIXED", "Wrote optimal agent preferences")
            return

        try:
            proto_bytes = base64.b64decode(raw)
        except Exception:
            report.add("Permissions", "ERROR", "agentPreferences is not valid base64")
            return

        all_ok = True

        for sentinel_key, (expected_val, label) in SENTINEL_KEYS.items():
            if sentinel_key not in proto_bytes:
                report.add(label, "WARN", "Sentinel key missing")
                all_ok = False
                continue
            idx = proto_bytes.index(sentinel_key) + len(sentinel_key)
            remaining = proto_bytes[idx:]
            b64_start = remaining.find(b"\x12\x06\n\x04")
            if b64_start >= 0:
                b64_val = remaining[b64_start + 4 : b64_start + 8].decode("ascii", errors="replace")
                policy_val = decode_policy_value(b64_val)
                if policy_val == expected_val:
                    report.add(label, "OK", f"Optimal (value={policy_val})")
                else:
                    report.add(label, "WARN", f"value={policy_val}, expected {expected_val}")
                    all_ok = False

        for sentinel_key, label in BOOL_SENTINEL_KEYS.items():
            if sentinel_key in proto_bytes:
                report.add(label, "OK", "Enabled")
            else:
                report.add(label, "WARN", "Not set (default=false)")
                all_ok = False

        if fix and not all_ok:
            set_key(conn, "antigravityUnifiedStateSync.agentPreferences", OPTIMAL_AGENT_PREFS_B64)
            report.add("Permission Fix", "FIXED", "Wrote optimal settings")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Check: Notification bloat
# ---------------------------------------------------------------------------
def check_notifications(report: Report, fix: bool = False) -> None:
    conn = open_db(paths.global_state_db)
    if conn is None:
        report.add("Notifications", "SKIP", "State DB not found")
        return

    try:
        raw = get_key(conn, "notifications.perSourceDoNotDisturbMode")
        if raw is None:
            report.add("Notifications", "OK", "No notification data")
            return

        try:
            data = json.loads(raw)
            count = len(data) if isinstance(data, (list, dict)) else 0
        except json.JSONDecodeError:
            report.add("Notifications", "WARN", f"Malformed JSON ({len(raw)} bytes)")
            return

        if count > 50:
            report.add("Notifications", "WARN", f"{count} entries (threshold: 50)")
            if fix and isinstance(data, list):
                cleaned = [e for e in data if isinstance(e, dict) and e.get("filter", 0) != 0]
                set_key(conn, "notifications.perSourceDoNotDisturbMode", json.dumps(cleaned))
                report.add("Notification Fix", "FIXED", f"Cleaned {count - len(cleaned)} entries")
        else:
            report.add("Notifications", "OK", f"{count} entries")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Check: VACUUM
# ---------------------------------------------------------------------------
def vacuum_databases(report: Report, skip: bool = False) -> None:
    dbs = [("Global State DB", paths.global_state_db)]

    for name, path in dbs:
        if not path.exists():
            report.add(f"VACUUM {name}", "SKIP", "File not found")
            continue
        size_before = db_size_kb(path)
        if skip:
            report.add(f"VACUUM {name}", "SKIP", f"{size_before:.1f} KB (--quick)")
            continue
        try:
            conn = sqlite3.connect(str(path))
            conn.execute("VACUUM")
            conn.close()
            size_after = db_size_kb(path)
            saved = size_before - size_after
            if saved > 1:
                report.add(f"VACUUM {name}", "FIXED",
                           f"{size_before:.1f} -> {size_after:.1f} KB (saved {saved:.1f} KB)")
            else:
                report.add(f"VACUUM {name}", "OK", f"{size_after:.1f} KB (compact)")
        except sqlite3.OperationalError as e:
            if "locked" in str(e):
                report.add(f"VACUUM {name}", "WARN", f"DB locked -- {size_before:.1f} KB")
            else:
                report.add(f"VACUUM {name}", "ERROR", str(e))


# ---------------------------------------------------------------------------
# Check: Log cleanup
# ---------------------------------------------------------------------------
def clean_logs(report: Report, keep: int = 2, skip: bool = False) -> None:
    if not paths.logs_dir.exists():
        report.add("Log Cleanup", "SKIP", "Log directory not found")
        return

    log_dirs = sorted(
        [d for d in paths.logs_dir.iterdir() if d.is_dir() and d.name[0].isdigit()],
        key=lambda d: d.name, reverse=True,
    )

    if len(log_dirs) <= keep:
        report.add("Log Cleanup", "OK", f"{len(log_dirs)} log dirs")
        return
    if skip:
        report.add("Log Cleanup", "SKIP", f"{len(log_dirs)} dirs, {len(log_dirs) - keep} removable")
        return

    removed = 0
    total_freed = 0
    for d in log_dirs[keep:]:
        try:
            dir_size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(str(d))
            removed += 1
            total_freed += dir_size
        except (PermissionError, OSError) as e:
            report.add("Log Cleanup", "WARN", f"Cannot remove {d.name}: {e}")

    if removed:
        report.add("Log Cleanup", "FIXED",
                    f"Removed {removed} dirs ({total_freed / 1024:.1f} KB freed)")


# ---------------------------------------------------------------------------
# Check: Extensions
# ---------------------------------------------------------------------------
def check_extensions(report: Report) -> None:
    ext_json = paths.extensions_dir / "extensions.json"
    if not ext_json.exists():
        report.add("Extensions", "SKIP", "extensions.json not found")
        return

    try:
        with open(ext_json, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        report.add("Extensions", "ERROR", f"Cannot read: {e}")
        return

    registered = {ext.get("relativeLocation", "") for ext in registry if ext.get("relativeLocation")}
    on_disk = {item.name for item in paths.extensions_dir.iterdir() if item.is_dir()}

    orphaned = on_disk - registered
    missing = registered - on_disk

    issues = []
    if orphaned:
        issues.append(f"{len(orphaned)} orphaned dirs")
    if missing:
        issues.append(f"{len(missing)} missing dirs")

    if issues:
        report.add("Extensions", "WARN", "; ".join(issues))
    else:
        report.add("Extensions", "OK", f"{len(registered)} registered, all present")


# ---------------------------------------------------------------------------
# Check: Settings
# ---------------------------------------------------------------------------
def check_settings(report: Report) -> None:
    if not paths.settings_json.exists():
        report.add("Settings", "SKIP", "settings.json not found")
        return

    try:
        with open(paths.settings_json, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        report.add("Settings", "ERROR", f"Cannot read: {e}")
        return

    try:
        baseline = load_baseline()
        critical = baseline.get("settings_critical", {})
    except FileNotFoundError:
        # Fallback to sensible defaults
        critical = {
            "telemetry.telemetryLevel": "off",
            "extensions.autoUpdate": False,
            "extensions.autoCheckUpdates": False,
            "editor.minimap.enabled": False,
            "workbench.startupEditor": "none",
            "workbench.enableExperiments": False,
        }

    issues = [
        f"{k}={settings.get(k)} (want {v})"
        for k, v in critical.items()
        if settings.get(k) != v
    ]

    if issues:
        for issue in issues:
            report.add("Settings", "WARN", issue)
    else:
        report.add("Settings", "OK", f"All {len(critical)} performance keys verified")


# ---------------------------------------------------------------------------
# Check: DB sizes
# ---------------------------------------------------------------------------
def check_db_sizes(report: Report) -> None:
    for name, path in [("Global State DB", paths.global_state_db)]:
        if path.exists():
            size = db_size_kb(path)
            status = "WARN" if size > 5000 else "OK"
            report.add(f"DB Size: {name}", status, f"{size:.1f} KB")
        else:
            report.add(f"DB Size: {name}", "SKIP", "Not found")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="OTTO Antigravity optimizer")
    parser.add_argument("--quick", action="store_true", help="Skip vacuums and log cleanup")
    parser.add_argument("--fix", action="store_true", help="Auto-fix issues found")
    args = parser.parse_args()

    report = Report()

    print("OTTO Optimizer -- scanning...")
    print()

    check_permissions(report, fix=args.fix)
    check_notifications(report, fix=args.fix)
    vacuum_databases(report, skip=args.quick)
    clean_logs(report, keep=2, skip=args.quick)
    check_extensions(report)
    check_settings(report)
    check_db_sizes(report)

    report.print_report()
    return 1 if report.has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
