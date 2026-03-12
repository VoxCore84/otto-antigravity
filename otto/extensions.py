"""
otto.extensions -- Re-disable bundled extensions after Antigravity updates.

Reads the disable manifest from the baseline and renames extension directories
that were restored by an update back to .disabled.

Usage:
    python -m otto.extensions          # Check and fix
    python -m otto.extensions --check  # Dry run
"""

from __future__ import annotations

import argparse
import sys

from .core import load_baseline, paths


def scan_extensions(
    manifest: list[str], check_only: bool = False
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Scan bundled extensions against manifest.

    Returns (fixed, already_disabled, not_found, failed).
    """
    fixed: list[str] = []
    already_disabled: list[str] = []
    not_found: list[str] = []
    failed: list[str] = []

    ext_dir = paths.bundled_ext_dir
    if not ext_dir.exists():
        print(f"[ERROR] Extensions directory not found: {ext_dir}", file=sys.stderr)
        sys.exit(2)

    for name in manifest:
        enabled = ext_dir / name
        disabled = ext_dir / f"{name}.disabled"

        if disabled.exists():
            already_disabled.append(name)
        elif enabled.exists():
            if check_only:
                fixed.append(name)
            else:
                try:
                    enabled.rename(disabled)
                    fixed.append(name)
                except PermissionError:
                    failed.append(f"{name} (PermissionError)")
                except OSError as e:
                    failed.append(f"{name} ({e})")
        else:
            not_found.append(name)

    return fixed, already_disabled, not_found, failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-disable bundled Antigravity extensions after update"
    )
    parser.add_argument("--check", action="store_true", help="Dry run -- report only")
    args = parser.parse_args()

    try:
        baseline = load_baseline()
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    manifest = baseline.get("disabled_extensions", [])
    if not manifest:
        print("[WARN] disabled_extensions list is empty in baseline", file=sys.stderr)
        return 0

    print(f"Loaded {len(manifest)} extensions from manifest", file=sys.stderr)

    fixed, already_disabled, not_found, failed = scan_extensions(
        manifest, check_only=args.check
    )

    action = "NEED DISABLING" if args.check else "RE-DISABLED"
    if fixed:
        print(f"  [{action}] ({len(fixed)}):", file=sys.stderr)
        for name in sorted(fixed):
            print(f"    - {name}", file=sys.stderr)
    if already_disabled:
        print(f"  [ALREADY DISABLED] ({len(already_disabled)})", file=sys.stderr)
    if not_found:
        print(f"  [NOT FOUND] ({len(not_found)})", file=sys.stderr)
    if failed:
        print(f"  [FAILED] ({len(failed)}):", file=sys.stderr)
        for entry in failed:
            print(f"    - {entry}", file=sys.stderr)

    print(
        f"Summary: {len(already_disabled)}/{len(manifest)} disabled, "
        f"{len(fixed)} {'need fix' if args.check else 'fixed'}, "
        f"{len(not_found)} not found, {len(failed)} failed",
        file=sys.stderr,
    )

    return 1 if (fixed or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
