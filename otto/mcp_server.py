"""
otto.mcp_server -- OTTO MCP server for Antigravity self-healing.

Exposes 4 MCP tools so the AI agent running inside Antigravity can
monitor and fix its own IDE's optimization state.

Tools:
    otto_health_check     -- Run all health checks (read-only)
    otto_fix_regressions  -- Auto-fix detected regressions
    otto_get_baseline     -- Return current baseline (read-only)
    otto_update_baseline  -- Snapshot current state as new baseline

Usage (stdio transport):
    python -m otto.mcp_server
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

# Force UTF-8 for stdio on Windows
if sys.platform == "win32":
    for stream in [sys.stdin, sys.stdout, sys.stderr]:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from fastmcp import FastMCP

from .core import (
    BOOL_SENTINEL_KEYS,
    OPTIMAL_AGENT_PREFS_B64,
    SENTINEL_KEYS,
    decode_policy_value,
    get_key,
    load_baseline,
    open_db,
    paths,
    save_baseline,
    set_key,
)


def _log(msg: str) -> None:
    print(f"[otto] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Health checks (reuse core constants, no duplication)
# ---------------------------------------------------------------------------

def _check_settings(baseline: dict) -> list[dict]:
    results = []
    critical = baseline.get("settings_critical", {})

    if not paths.settings_json.exists():
        return [{"name": "settings.json", "status": "ERROR", "detail": "File not found"}]

    try:
        with open(paths.settings_json, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return [{"name": "settings.json", "status": "ERROR", "detail": str(e)}]

    mismatches = [f"{k}: got {settings.get(k)!r}, want {v!r}" for k, v in critical.items() if settings.get(k) != v]
    if mismatches:
        results.append({"name": "settings.json", "status": "WARN", "detail": f"{len(mismatches)} mismatches"})
    else:
        results.append({"name": "settings.json", "status": "OK", "detail": f"{len(critical)} keys match"})
    return results


def _check_argv(baseline: dict) -> list[dict]:
    results = []
    critical = baseline.get("argv_critical", {})
    required_flags = baseline.get("argv_js_flags_required", [])

    if not paths.argv_json.exists():
        return [{"name": "argv.json", "status": "ERROR", "detail": "File not found"}]

    try:
        with open(paths.argv_json, "r", encoding="utf-8") as f:
            argv = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return [{"name": "argv.json", "status": "ERROR", "detail": str(e)}]

    key_issues = [f"{k}: got {argv.get(k)!r}" for k, v in critical.items() if argv.get(k) != v]
    if key_issues:
        results.append({"name": "argv.json keys", "status": "WARN", "detail": "; ".join(key_issues)})
    else:
        results.append({"name": "argv.json keys", "status": "OK", "detail": f"{len(critical)} keys match"})

    js_flags = argv.get("js-flags", "")
    missing = [f for f in required_flags if f not in js_flags]
    if missing:
        results.append({"name": "argv.json js-flags", "status": "WARN", "detail": f"Missing: {', '.join(missing)}"})
    else:
        results.append({"name": "argv.json js-flags", "status": "OK", "detail": f"{len(required_flags)} flags present"})

    return results


def _check_extensions(baseline: dict) -> list[dict]:
    manifest = baseline.get("disabled_extensions", [])
    if not paths.bundled_ext_dir.exists():
        return [{"name": "disabled_extensions", "status": "ERROR", "detail": "Dir not found"}]

    re_enabled = []
    still_disabled = 0
    for name in manifest:
        if (paths.bundled_ext_dir / f"{name}.disabled").exists():
            still_disabled += 1
        elif (paths.bundled_ext_dir / name).exists():
            re_enabled.append(name)

    if re_enabled:
        return [{"name": "disabled_extensions", "status": "WARN",
                 "detail": f"{len(re_enabled)} re-enabled: {', '.join(re_enabled[:5])}"}]
    return [{"name": "disabled_extensions", "status": "OK",
             "detail": f"{still_disabled}/{len(manifest)} disabled"}]


def _check_db_permissions(baseline: dict) -> list[dict]:
    results = []
    conn = open_db(paths.global_state_db)
    if conn is None:
        return [{"name": "db_permissions", "status": "ERROR", "detail": "State DB not found"}]

    try:
        raw = get_key(conn, "antigravityUnifiedStateSync.agentPreferences")
        if raw is None:
            return [{"name": "db_permissions", "status": "WARN", "detail": "agentPreferences missing"}]

        try:
            proto_bytes = base64.b64decode(raw)
        except Exception:
            return [{"name": "db_permissions", "status": "ERROR", "detail": "Invalid base64"}]

        all_ok = True
        for sentinel_key, (expected, label) in SENTINEL_KEYS.items():
            if sentinel_key not in proto_bytes:
                results.append({"name": f"db_perm: {label}", "status": "WARN", "detail": "Missing"})
                all_ok = False
                continue
            idx = proto_bytes.index(sentinel_key) + len(sentinel_key)
            remaining = proto_bytes[idx:]
            b64_start = remaining.find(b"\x12\x06\n\x04")
            if b64_start >= 0:
                b64_val = remaining[b64_start + 4 : b64_start + 8].decode("ascii", errors="replace")
                val = decode_policy_value(b64_val)
                if val == expected:
                    results.append({"name": f"db_perm: {label}", "status": "OK", "detail": f"value={val}"})
                else:
                    results.append({"name": f"db_perm: {label}", "status": "WARN", "detail": f"value={val}, want {expected}"})
                    all_ok = False

        for sentinel_key, label in BOOL_SENTINEL_KEYS.items():
            if sentinel_key in proto_bytes:
                results.append({"name": f"db_perm: {label}", "status": "OK", "detail": "Enabled"})
            else:
                results.append({"name": f"db_perm: {label}", "status": "WARN", "detail": "Not set"})
                all_ok = False

        overall = "OK" if all_ok else "WARN"
        results.append({"name": "db_permissions (overall)", "status": overall,
                        "detail": "All optimal" if all_ok else "Sub-optimal"})
    finally:
        conn.close()
    return results


def _check_journal_mode(baseline: dict) -> list[dict]:
    expected = baseline.get("db_journal_mode", "wal")
    conn = open_db(paths.global_state_db)
    if conn is None:
        return [{"name": "db_journal_mode", "status": "ERROR", "detail": "State DB not found"}]
    try:
        actual = conn.execute("PRAGMA journal_mode").fetchone()[0]
        status = "OK" if actual == expected else "WARN"
        return [{"name": "db_journal_mode", "status": status, "detail": f"journal_mode={actual}"}]
    except sqlite3.Error as e:
        return [{"name": "db_journal_mode", "status": "ERROR", "detail": str(e)}]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fix actions
# ---------------------------------------------------------------------------

def _fix_db_permissions() -> list[dict]:
    conn = open_db(paths.global_state_db)
    if conn is None:
        return [{"name": "fix_db_permissions", "status": "ERROR", "detail": "State DB not found"}]
    try:
        set_key(conn, "antigravityUnifiedStateSync.agentPreferences", OPTIMAL_AGENT_PREFS_B64)
        return [{"name": "fix_db_permissions", "status": "FIXED", "detail": "Wrote optimal prefs"}]
    except sqlite3.Error as e:
        return [{"name": "fix_db_permissions", "status": "FAILED", "detail": str(e)}]
    finally:
        conn.close()


def _fix_extensions(baseline: dict) -> list[dict]:
    results = []
    manifest = baseline.get("disabled_extensions", [])
    if not paths.bundled_ext_dir.exists():
        return [{"name": "fix_extensions", "status": "ERROR", "detail": "Dir not found"}]

    for name in manifest:
        enabled = paths.bundled_ext_dir / name
        disabled = paths.bundled_ext_dir / f"{name}.disabled"
        if enabled.exists() and not disabled.exists():
            try:
                enabled.rename(disabled)
                results.append({"name": f"ext: {name}", "status": "FIXED", "detail": "Re-disabled"})
            except OSError as e:
                results.append({"name": f"ext: {name}", "status": "FAILED", "detail": str(e)})

    return results or [{"name": "fix_extensions", "status": "OK", "detail": "No extensions needed re-disabling"}]


def _fix_journal_mode(baseline: dict) -> list[dict]:
    expected = baseline.get("db_journal_mode", "wal")
    conn = open_db(paths.global_state_db)
    if conn is None:
        return [{"name": "fix_journal_mode", "status": "ERROR", "detail": "State DB not found"}]
    try:
        actual = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if actual == expected:
            return [{"name": "fix_journal_mode", "status": "OK", "detail": f"Already {expected}"}]
        conn.execute(f"PRAGMA journal_mode={expected}")
        return [{"name": "fix_journal_mode", "status": "FIXED", "detail": f"{actual} -> {expected}"}]
    except sqlite3.Error as e:
        return [{"name": "fix_journal_mode", "status": "FAILED", "detail": str(e)}]
    finally:
        conn.close()


def _snapshot_current_state() -> dict:
    snapshot: dict = {"version": 1, "created": str(date.today())}

    if paths.settings_json.exists():
        try:
            with open(paths.settings_json, "r", encoding="utf-8") as f:
                settings = json.load(f)
            try:
                old = load_baseline()
                keys = list(old.get("settings_critical", {}).keys())
            except FileNotFoundError:
                keys = ["telemetry.telemetryLevel", "extensions.autoUpdate", "extensions.autoCheckUpdates",
                        "editor.minimap.enabled", "workbench.startupEditor", "workbench.enableExperiments"]
            snapshot["settings_critical"] = {k: settings.get(k) for k in keys if k in settings}
        except (json.JSONDecodeError, OSError):
            snapshot["settings_critical"] = {}
    else:
        snapshot["settings_critical"] = {}

    if paths.argv_json.exists():
        try:
            with open(paths.argv_json, "r", encoding="utf-8") as f:
                argv = json.load(f)
            argv_keys = ["disable-telemetry", "enable-crash-reporter",
                         "disable-renderer-backgrounding", "disable-background-timer-throttling"]
            snapshot["argv_critical"] = {k: argv.get(k) for k in argv_keys if k in argv}
            js_flags = argv.get("js-flags", "")
            snapshot["argv_js_flags_required"] = js_flags.split() if js_flags else []
        except (json.JSONDecodeError, OSError):
            snapshot["argv_critical"] = {}
            snapshot["argv_js_flags_required"] = []
    else:
        snapshot["argv_critical"] = {}
        snapshot["argv_js_flags_required"] = []

    if paths.bundled_ext_dir.exists():
        snapshot["disabled_extensions"] = sorted(
            item.name.removesuffix(".disabled")
            for item in paths.bundled_ext_dir.iterdir()
            if item.is_dir() and item.name.endswith(".disabled")
        )
    else:
        snapshot["disabled_extensions"] = []

    conn = open_db(paths.global_state_db)
    if conn is not None:
        try:
            raw = get_key(conn, "antigravityUnifiedStateSync.agentPreferences")
            snapshot["db_permissions"] = {"agent_prefs_b64": raw or ""}
            row = conn.execute("PRAGMA journal_mode").fetchone()
            snapshot["db_journal_mode"] = row[0] if row else "unknown"
        except sqlite3.Error:
            snapshot["db_permissions"] = {"agent_prefs_b64": ""}
            snapshot["db_journal_mode"] = "unknown"
        finally:
            conn.close()
    else:
        snapshot["db_permissions"] = {"agent_prefs_b64": ""}
        snapshot["db_journal_mode"] = "unknown"

    return snapshot


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("otto", instructions="OTTO: Antigravity optimization health monitor.")


@mcp.tool(annotations={"readOnlyHint": True},
          description="Run all OTTO health checks. Returns JSON with status, score, and per-check details.")
def otto_health_check() -> dict:
    try:
        baseline = load_baseline()
    except FileNotFoundError as e:
        return {"status": "critical", "score": "0/0", "checks": [{"name": "baseline", "status": "ERROR", "detail": str(e)}]}

    checks: list[dict] = []
    checks.extend(_check_settings(baseline))
    checks.extend(_check_argv(baseline))
    checks.extend(_check_extensions(baseline))
    checks.extend(_check_db_permissions(baseline))
    checks.extend(_check_journal_mode(baseline))

    total = len(checks)
    ok = sum(1 for c in checks if c["status"] == "OK")
    errors = sum(1 for c in checks if c["status"] == "ERROR")
    warns = sum(1 for c in checks if c["status"] == "WARN")

    status = "critical" if errors else ("degraded" if warns else "healthy")
    return {"status": status, "score": f"{ok}/{total}", "checks": checks}


@mcp.tool(description="Auto-fix detected optimization regressions.")
def otto_fix_regressions() -> dict:
    try:
        baseline = load_baseline()
    except FileNotFoundError as e:
        return {"fixed": [], "already_ok": [], "failed": [{"name": "baseline", "detail": str(e)}]}

    fixed, already_ok, failed = [], [], []

    perm_checks = _check_db_permissions(baseline)
    perm_overall = [c for c in perm_checks if "overall" in c["name"]]
    if perm_overall and perm_overall[0]["status"] != "OK":
        for r in _fix_db_permissions():
            (fixed if r["status"] == "FIXED" else already_ok if r["status"] == "OK" else failed).append(r)
    else:
        already_ok.append({"name": "db_permissions", "status": "OK", "detail": "Already optimal"})

    for r in _fix_extensions(baseline):
        (fixed if r["status"] == "FIXED" else already_ok if r["status"] == "OK" else failed).append(r)

    for r in _fix_journal_mode(baseline):
        (fixed if r["status"] == "FIXED" else already_ok if r["status"] == "OK" else failed).append(r)

    return {"fixed": fixed, "already_ok": already_ok, "failed": failed}


@mcp.tool(annotations={"readOnlyHint": True}, description="Return the current OTTO baseline JSON.")
def otto_get_baseline() -> dict:
    try:
        return load_baseline()
    except FileNotFoundError as e:
        return {"error": str(e)}


@mcp.tool(description="Snapshot current Antigravity state as the new OTTO baseline.")
def otto_update_baseline() -> dict:
    try:
        snapshot = _snapshot_current_state()
        save_baseline(snapshot)
        return {"status": "saved", "created": snapshot.get("created"),
                "settings_count": len(snapshot.get("settings_critical", {})),
                "disabled_extensions_count": len(snapshot.get("disabled_extensions", []))}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def main():
    _log("Starting OTTO MCP server...")
    try:
        baseline = load_baseline()
        _log(f"Baseline loaded: v{baseline.get('version')}, created {baseline.get('created')}")
    except FileNotFoundError:
        _log("WARNING: No baseline file found")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
