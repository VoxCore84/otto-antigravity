"""
otto.patch_mcp -- Patch Antigravity to auto-confirm MCP tool calls.

Antigravity hardcodes MCP tool confirmation (the "Run MCP tool call?" dialog).
There is NO setting, sentinel key, or config flag to disable it. The WAITING
status comes from the Gemini server, and the client always renders the dialog
via the `Uhn` React component in `jetskiAgent/main.js`.

This module patches the compiled JS to auto-confirm immediately and render
nothing, so MCP tools execute without user intervention.

The patch must be re-applied after every Antigravity update.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .core import paths

# The original Uhn component -- renders "Run MCP tool call?" with Accept/Reject.
# This exact string is from the compiled JS. If Antigravity updates change this,
# the patch will fail gracefully (no match = no modification).
ORIGINAL = (
    'Uhn=({trajectoryId:e,stepIndex:t})=>{let{cascadeContext:{events:'
    '{sendUserInteraction:r}}}=Wn(),n=a=>{r(ur(tE,{trajectoryId:e,'
    'stepIndex:t,interaction:{case:"mcp",value:ur(Lja,{confirm:a})}}))}'
    ';return A("div",{className:"p-2 border-t border-gray-500/25 text-sm"'
    ',children:A("div",{className:"flex w-full items-center justify-between'
    ' flex-wrap",children:[A("p",{children:"Run MCP tool call?"}),A("div",'
    '{className:"flex flex-row gap-x-2 ml-auto",children:[A("button",'
    '{onClick:()=>{n(!1)},className:"hover:opacity-100 cursor-pointer '
    'rounded-md text-sm transition-[opacity] opacity-60 hover:opacity-100"'
    ',children:"Reject"}),A("button",{onClick:()=>{n(!0)},className:'
    '"hover:bg-ide-button-hover-background cursor-pointer rounded-md '
    'bg-ide-button-background px-1 py-px text-sm text-ide-button-color '
    'transition-[background]",children:"Accept"})]})]})})}'
)

# Patched Uhn -- immediately sends confirm:true, renders nothing.
PATCHED = (
    'Uhn=({trajectoryId:e,stepIndex:t})=>{let{cascadeContext:{events:'
    '{sendUserInteraction:r}}}=Wn(),n=a=>{r(ur(tE,{trajectoryId:e,'
    'stepIndex:t,interaction:{case:"mcp",value:ur(Lja,{confirm:a})}}))}'
    ';n(!0);return null}'
)

PATCH_MARKER = ';n(!0);return null}'


def check_status(content: str) -> str:
    """Return 'patched', 'original', or 'unknown'."""
    if PATCH_MARKER in content and ORIGINAL not in content:
        return "patched"
    if ORIGINAL in content:
        return "original"
    return "unknown"


def get_main_js() -> Path:
    """Return the path to jetskiAgent/main.js."""
    return paths.main_js


def apply_patch(main_js: Path | None = None) -> str:
    """Apply the MCP auto-confirm patch. Returns status message."""
    js = main_js or get_main_js()

    if not js.is_file():
        return f"[ERROR] main.js not found: {js}"

    content = js.read_text(encoding="utf-8")
    status = check_status(content)

    if status == "patched":
        return "[OK] Already patched."
    if status == "unknown":
        return (
            "[ERROR] Original pattern not found in main.js. "
            "Antigravity may have updated. Manual inspection needed."
        )

    # Create backup
    backup = js.with_suffix(".js.bak")
    if not backup.exists():
        shutil.copy2(js, backup)

    new_content = content.replace(ORIGINAL, PATCHED, 1)
    js.write_text(new_content, encoding="utf-8")

    # Verify
    verify = js.read_text(encoding="utf-8")
    if check_status(verify) == "patched":
        saved = len(ORIGINAL) - len(PATCHED)
        return f"[OK] Patch applied ({saved} bytes saved). Restart Antigravity."
    return "[ERROR] Verification failed after write."


def revert_patch(main_js: Path | None = None) -> str:
    """Revert to backup. Returns status message."""
    js = main_js or get_main_js()
    backup = js.with_suffix(".js.bak")
    if not backup.exists():
        return "[ERROR] No backup file found."
    shutil.copy2(backup, js)
    return "[OK] Reverted to backup."


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch Antigravity MCP auto-confirm"
    )
    parser.add_argument("--check", action="store_true", help="Check patch status")
    parser.add_argument("--revert", action="store_true", help="Restore from backup")
    args = parser.parse_args()

    js = get_main_js()

    if args.revert:
        print(revert_patch(js))
        return 0

    if args.check:
        if not js.is_file():
            print(f"[ERROR] main.js not found: {js}")
            return 1
        status = check_status(js.read_text(encoding="utf-8"))
        if status == "patched":
            print("[OK] MCP auto-confirm patch is ACTIVE.")
        elif status == "original":
            print("[WARN] Patch is NOT applied. Run without --check to apply.")
        else:
            print("[WARN] Unknown state -- Antigravity may have updated.")
        return 0

    print(apply_patch(js))
    return 0


if __name__ == "__main__":
    sys.exit(main())
