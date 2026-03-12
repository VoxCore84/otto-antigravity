"""
otto.core -- Shared infrastructure for the OTTO toolkit.

Cross-platform path resolution, SQLite helpers, protobuf decoder,
permission policy constants, and reporting.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Cross-platform path resolution
# ---------------------------------------------------------------------------

def _home() -> Path:
    """Return user home directory."""
    return Path.home()


def _detect_install_dir() -> Path:
    """Detect Antigravity installation directory.

    Checks in order:
      1. ANTIGRAVITY_INSTALL_DIR env var (explicit override)
      2. Platform-specific default locations
    """
    env = os.environ.get("ANTIGRAVITY_INSTALL_DIR")
    if env:
        return Path(env)

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", str(_home() / "AppData" / "Local"))
        return Path(local) / "Programs" / "Antigravity"
    elif sys.platform == "darwin":
        # macOS: check user-local first, then system
        user_app = _home() / "Applications" / "Antigravity.app" / "Contents"
        if user_app.exists():
            return user_app
        return Path("/Applications/Antigravity.app/Contents")
    else:
        # Linux
        for candidate in [
            Path("/usr/share/antigravity"),
            Path("/opt/antigravity"),
            _home() / ".local" / "share" / "antigravity",
        ]:
            if candidate.exists():
                return candidate
        return Path("/usr/share/antigravity")


def _detect_data_dir() -> Path:
    """Detect Antigravity user data directory (settings, state DBs, logs)."""
    env = os.environ.get("ANTIGRAVITY_DATA_DIR")
    if env:
        return Path(env)

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(_home() / "AppData" / "Roaming"))
        return Path(appdata) / "Antigravity"
    elif sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "Antigravity"
    else:
        return _home() / ".config" / "Antigravity"


def _detect_extensions_dir() -> Path:
    """Detect user extensions directory."""
    env = os.environ.get("ANTIGRAVITY_EXTENSIONS_DIR")
    if env:
        return Path(env)

    if sys.platform == "win32":
        return _home() / ".antigravity" / "extensions"
    elif sys.platform == "darwin":
        return _home() / ".antigravity" / "extensions"
    else:
        return _home() / ".antigravity" / "extensions"


@dataclass
class AntigravityPaths:
    """All Antigravity-related paths, resolved for the current platform."""

    install_dir: Path
    data_dir: Path
    user_dir: Path
    extensions_dir: Path
    bundled_ext_dir: Path
    global_state_db: Path
    settings_json: Path
    argv_json: Path
    logs_dir: Path
    main_js: Path

    @classmethod
    def detect(cls) -> AntigravityPaths:
        """Auto-detect all paths for the current platform."""
        install = _detect_install_dir()
        data = _detect_data_dir()
        user = data / "User"
        extensions = _detect_extensions_dir()

        if sys.platform == "win32":
            bundled = install / "resources" / "app" / "extensions"
            main_js = install / "resources" / "app" / "out" / "jetskiAgent" / "main.js"
            argv = _home() / ".antigravity" / "argv.json"
        elif sys.platform == "darwin":
            bundled = install / "Resources" / "app" / "extensions"
            main_js = install / "Resources" / "app" / "out" / "jetskiAgent" / "main.js"
            argv = _home() / ".antigravity" / "argv.json"
        else:
            bundled = install / "resources" / "app" / "extensions"
            main_js = install / "resources" / "app" / "out" / "jetskiAgent" / "main.js"
            argv = _home() / ".antigravity" / "argv.json"

        return cls(
            install_dir=install,
            data_dir=data,
            user_dir=user,
            extensions_dir=extensions,
            bundled_ext_dir=bundled,
            global_state_db=user / "globalStorage" / "state.vscdb",
            settings_json=user / "settings.json",
            argv_json=argv,
            logs_dir=data / "logs",
            main_js=main_js,
        )


# Module-level singleton
paths = AntigravityPaths.detect()


# ---------------------------------------------------------------------------
# Baseline file location
# ---------------------------------------------------------------------------

def baseline_path() -> Path:
    """Return path to otto_baseline.json (next to this module, or cwd)."""
    # Check next to the package first
    pkg_dir = Path(__file__).parent
    candidate = pkg_dir / "baseline.json"
    if candidate.exists():
        return candidate
    # Fall back to cwd
    cwd = Path.cwd() / "baseline.json"
    if cwd.exists():
        return cwd
    # Default to package dir (for creation)
    return candidate


def load_baseline() -> dict:
    """Load baseline.json. Raises FileNotFoundError if missing."""
    bp = baseline_path()
    if not bp.exists():
        raise FileNotFoundError(f"Baseline not found: {bp}")
    with open(bp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_baseline(data: dict) -> None:
    """Write baseline.json atomically."""
    bp = baseline_path()
    tmp = bp.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(bp)


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def open_db(path: Path) -> sqlite3.Connection | None:
    """Open a SQLite DB, return None if file doesn't exist."""
    if not path.exists():
        return None
    try:
        return sqlite3.connect(str(path), timeout=5)
    except sqlite3.OperationalError:
        return None


def get_key(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a value from the ItemTable."""
    row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_key(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a value in the ItemTable."""
    conn.execute(
        "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def db_size_kb(path: Path) -> float:
    """Return file size in KB."""
    if path.exists():
        return path.stat().st_size / 1024
    return 0.0


# ---------------------------------------------------------------------------
# Protobuf helpers (for reading/writing Antigravity's permission blobs)
# ---------------------------------------------------------------------------

def decode_policy_value(sentinel_b64: str) -> int | None:
    """Decode a protobuf varint from a base64 sentinel value.

    The sentinel is stored as base64-encoded protobuf.
    Format: field tag (0x10 = field 2 varint) + varint value.
    """
    try:
        data = base64.b64decode(sentinel_b64)
        if len(data) >= 2 and data[0] == 0x10:
            return data[1]
        if len(data) >= 2 and data[0] == 0x08:
            return data[1]
    except Exception:
        pass
    return None


def build_proto_varint(field_num: int, value: int) -> bytes:
    """Build a protobuf varint field."""
    tag = (field_num << 3) | 0
    result = bytes([tag])
    while value > 0x7F:
        result += bytes([(value & 0x7F) | 0x80])
        value >>= 7
    result += bytes([value])
    return result


def build_proto_string(field_num: int, s: str) -> bytes:
    """Build a protobuf length-delimited string field."""
    encoded = s.encode("utf-8")
    return _build_proto_ld(field_num, encoded)


def build_proto_bytes(field_num: int, data: bytes) -> bytes:
    """Build a protobuf length-delimited bytes field."""
    return _build_proto_ld(field_num, data)


def _build_proto_ld(field_num: int, data: bytes) -> bytes:
    tag = (field_num << 3) | 2
    length = len(data)
    len_bytes = b""
    while length > 0x7F:
        len_bytes += bytes([(length & 0x7F) | 0x80])
        length >>= 7
    len_bytes += bytes([length])
    return bytes([tag]) + len_bytes + data


# ---------------------------------------------------------------------------
# Permission policy constants
# ---------------------------------------------------------------------------

# Terminal enum (Jd): OFF=1, AUTO=2, EAGER=3  (3 is max, NOT 4!)
# Artifact enum (C0): ALWAYS=1, TURBO=2, AUTO=3
# Planning enum (RI): UNSPECIFIED=0, OFF=1, ON=2
TERMINAL_EAGER = 3
ARTIFACT_TURBO = 2
PLANNING_OFF = 1

# Sentinel key names (byte strings for scanning protobuf blobs)
SENTINEL_KEYS = {
    b"terminalAutoExecutionPolicySentinelKey": (TERMINAL_EAGER, "Terminal EAGER(3)"),
    b"artifactReviewPolicySentinelKey": (ARTIFACT_TURBO, "Artifact TURBO(2)"),
    b"planningModeSentinelKey": (PLANNING_OFF, "Planning OFF(1)"),
}

BOOL_SENTINEL_KEYS = {
    b"allowAgentAccessNonWorkspaceFilesSentinelKey": "Non-Workspace Access",
    b"allowCascadeAccessGitignoreFilesSentinelKey": "Gitignore Access",
    b"followAlongWithAgentDefaultSentinelKey": "Follow Along",
}

# Pre-built optimal agentPreferences protobuf blob (base64).
# Contains: terminal=EAGER(3), artifact=TURBO(2), planning=OFF(1),
#           non-workspace=true, gitignore=true, followAlong=true
OPTIMAL_AGENT_PREFS_B64 = (
    "CjAKJnRlcm1pbmFsQXV0b0V4ZWN1dGlvblBvbGljeVNlbnRpbmVsS2V5EgYKBEVBTT0"
    "KKQofYXJ0aWZhY3RSZXZpZXdQb2xpY3lTZW50aW5lbEtleRIGCgRFQUk9"
    "CiEKF3BsYW5uaW5nTW9kZVNlbnRpbmVsS2V5EgYKBEVBRT0"
    "KNgosYWxsb3dBZ2VudEFjY2Vzc05vbldvcmtzcGFjZUZpbGVzU2VudGluZWxLZXkSBgoEQ0FFPQo1"
    "CithbGxvd0Nhc2NhZGVBY2Nlc3NHaXRpZ25vcmVGaWxlc1NlbnRpbmVsS2V5EgYKBENBRT0"
    "KMAomZm9sbG93QWxvbmdXaXRoQWdlbnREZWZhdWx0U2VudGluZWxLZXkSBgoEQ0FFPQ=="
)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    status: str  # "OK", "WARN", "FIXED", "ERROR", "SKIP"
    detail: str = ""


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name, status, detail))

    def print_report(self) -> None:
        print()
        print("=" * 60)
        print("  OTTO Antigravity Health Report")
        print("=" * 60)
        print()
        max_name = max((len(r.name) for r in self.results), default=20)
        for r in self.results:
            icon = {
                "OK": "[OK]", "WARN": "[!!]", "FIXED": "[FX]",
                "ERROR": "[ER]", "SKIP": "[--]",
            }.get(r.status, "[??]")
            print(f"  {icon} {r.name:<{max_name}}  {r.detail}")
        print()
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        parts = [f"{counts[s]} {s}" for s in ["OK", "FIXED", "WARN", "ERROR", "SKIP"] if s in counts]
        print(f"  Summary: {', '.join(parts)}")
        print("=" * 60)
        print()

    @property
    def has_errors(self) -> bool:
        return any(r.status == "ERROR" for r in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(r.status == "WARN" for r in self.results)

    def to_dicts(self) -> list[dict]:
        return [{"name": r.name, "status": r.status, "detail": r.detail} for r in self.results]
