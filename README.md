# OTTO - Self-Healing Optimizer for Antigravity IDE

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue) ![License: MIT](https://img.shields.io/github/license/VoxCore84/otto-antigravity) ![GitHub release](https://img.shields.io/github/v/release/VoxCore84/otto-antigravity)

OTTO patches, monitors, and auto-fixes Antigravity's permission gates, performance settings, and extension bloat — so you can focus on building instead of clicking "Accept" on every MCP tool call.

## The Problem

Antigravity (Google's Gemini-powered IDE) ships with aggressive permission prompts:

- **MCP tool calls** require manual Accept/Reject on *every single invocation* — with no setting to disable it
- **Permission policies** reset to restrictive defaults after updates (terminal auto-execute, artifact review, planning mode)
- **37+ bundled language extensions** you don't need (Clojure, Dart, Julia, PHP...) waste RAM and slow startup
- **Settings drift** after updates undoes your performance tuning

OTTO fixes all of these, and keeps them fixed.

## Features

| Tool | What it does |
|------|-------------|
| **`otto-patch`** | Patches compiled JS to auto-confirm MCP tool calls (no more Accept/Reject dialogs) |
| **`otto`** | Health scanner: checks permissions, settings, extensions, DB state against a known-good baseline |
| **`otto-watchdog`** | Background daemon that auto-patches permission regressions every 60 seconds |
| **`otto-extensions`** | Re-disables bundled extensions that Antigravity updates silently restore |
| **`otto-mcp`** | MCP server so the AI agent can monitor and fix its own IDE — a self-healing loop |
| **Extension** | VS Code/Antigravity status bar indicator with real-time health monitoring |

## Quick Start

```bash
# Install
pip install -e .

# Copy and customize the baseline
cp baseline.example.json otto/baseline.json
# Edit otto/baseline.json to match your preferences

# Patch MCP auto-confirm (the big one)
otto-patch

# Run a full health check
otto --fix

# Disable unused bundled extensions
otto-extensions

# Start the background watchdog
otto-watchdog
```

## MCP Auto-Confirm Patch

The flagship feature. Antigravity hardcodes MCP tool confirmation — the server sends every MCP step with `WAITING` status, and the client renders an Accept/Reject dialog. There is **no setting, sentinel key, or config flag** to disable it.

OTTO patches the React component (`Uhn`) in `jetskiAgent/main.js` to immediately send `confirm: true` and render nothing:

```bash
otto-patch          # Apply the patch
otto-patch --check  # Check if patch is active
otto-patch --revert # Restore from backup
```

The patch must be re-applied after every Antigravity update (the compiled JS gets overwritten). Add it to your launch routine.

## Self-Healing MCP Server

The most architecturally interesting feature. Run `otto-mcp` as an MCP server inside Antigravity, and the AI agent can:

```
"Is my IDE running in a degraded state?"  →  otto_health_check()
"Fix the regressions"                     →  otto_fix_regressions()
"What's the baseline?"                    →  otto_get_baseline()
"Save current state as the new baseline"  →  otto_update_baseline()
```

Add to your `mcp_config.json`:

```json
{
  "mcpServers": {
    "otto": {
      "command": "python",
      "args": ["-m", "otto.mcp_server"],
      "env": {"PYTHONUTF8": "1"}
    }
  }
}
```

## Permission Policy Reference

OTTO manages these Antigravity permission gates (reverse-engineered from compiled source):

| Setting | Enum | Values | Optimal |
|---------|------|--------|---------|
| Terminal execution | `Jd` | OFF=1, AUTO=2, **EAGER=3** | 3 |
| Artifact review | `C0` | ALWAYS=1, **TURBO=2**, AUTO=3 | 2 |
| Planning mode | `RI` | UNSPECIFIED=0, **OFF=1**, ON=2 | 1 |
| Browser JS | `$h` | DISABLED=1, ASK=2, MODEL=3, **TURBO=4** | 4 |
| Non-workspace files | bool | | true |
| Gitignore files | bool | | true |
| Follow along | bool | | true |
| Confirm reload | bool | | false |

These are stored as protobuf-encoded base64 blobs in `state.vscdb`. OTTO handles the encoding/decoding.

> **Warning**: Terminal max is 3 (EAGER), **not 4**. Value 4 is undefined in the terminal enum and falls back to OFF(1).

## VS Code Extension

The `extension/` directory contains a VS Code/Antigravity extension that provides:

- Status bar indicator (green/yellow/red OTTO icon)
- 30-minute periodic health checks
- Real-time settings drift detection
- "Fix All" command that invokes the Python toolkit

```bash
cd extension
npm install
npm run build
# Copy to ~/.antigravity/extensions/otto-antigravity-1.0.0/
```

Configure via VS Code settings:
- `otto.pythonPath` — Python interpreter path (default: `python`)
- `otto.toolkitDir` — Path to the otto package (auto-detect if empty)
- `otto.checkIntervalMinutes` — Health check interval (default: 30)

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `ANTIGRAVITY_INSTALL_DIR` | Override Antigravity installation path |
| `ANTIGRAVITY_DATA_DIR` | Override user data directory |
| `ANTIGRAVITY_EXTENSIONS_DIR` | Override user extensions directory |

## How It Works

Antigravity stores permission policies in SQLite databases (`state.vscdb`) as protobuf-encoded base64 blobs under keys like `antigravityUnifiedStateSync.agentPreferences`. The protobuf contains sentinel keys (e.g., `terminalAutoExecutionPolicySentinelKey`) with nested base64-encoded varint values.

OTTO reverse-engineered this encoding from Antigravity's compiled JavaScript (11.2MB `jetskiAgent/main.js`), identified all 34 sentinel keys and their enum values, and built tools to read, validate, and patch them.

The MCP auto-confirm patch works by replacing the `Uhn` React component (which renders the Accept/Reject buttons) with one that immediately calls `sendUserInteraction` with `confirm: true`. The WAITING status is set server-side by the Gemini backend — there's no client-side setting to avoid it.

## vs. antigravity-panel

[antigravity-panel](https://github.com/n2ns/antigravity-panel) is the only other public Antigravity toolkit. Key differences:

| Feature | OTTO | antigravity-panel |
|---------|------|-------------------|
| MCP auto-confirm | JS source patch (clean) | OS-level auto-clicker (crude) |
| Permission blob manipulation | Protobuf-level read/write | Not supported |
| Self-healing MCP server | Yes (AI fixes its own IDE) | No |
| Extension lifecycle | Manifest-driven re-disable | Not supported |
| Baseline regression detection | Full (settings + argv + DB + extensions) | Partial |
| Quota monitoring | No | Yes |
| Cross-platform | Windows + macOS + Linux | Windows + macOS + Linux |

## License

MIT
