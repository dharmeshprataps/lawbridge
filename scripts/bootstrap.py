#!/usr/bin/env python3
"""Install an official, pinned Lean release inside this project only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "4.35.0"
TOOLS = ROOT / ".tools"
DEST = TOOLS / "lean"


def main() -> int:
    existing = DEST / "bin" / "lean"
    if existing.exists():
        result = subprocess.run([str(existing), "--version"], text=True, capture_output=True, check=True)
        if f"version {VERSION}" not in result.stdout:
            raise RuntimeError(f"Existing project toolchain is not Lean {VERSION}: {result.stdout.strip()}")
        print(result.stdout.strip())
        return 0

    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = platform.machine().lower()
    if system is None or arch not in {"arm64", "aarch64", "x86_64", "amd64"}:
        raise RuntimeError("Bootstrap supports macOS/Linux on ARM64 or x86-64.")
    suffix = "_aarch64" if arch in {"arm64", "aarch64"} else ""
    asset_name = f"lean-{VERSION}-{system}{suffix}.tar.zst"
    request = urllib.request.Request(
        f"https://api.github.com/repos/leanprover/lean4/releases/tags/v{VERSION}",
        headers={"User-Agent": "lawbridge-bootstrap", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next((a for a in release["assets"] if a["name"] == asset_name), None)
    if asset is None or not asset.get("digest", "").startswith("sha256:"):
        raise RuntimeError("Official release has no matching asset with a SHA-256 digest.")
    expected = asset["digest"].split(":", 1)[1]
    url = asset["browser_download_url"]
    if not url.startswith("https://github.com/leanprover/lean4/releases/download/"):
        raise RuntimeError("Unexpected release asset location.")
    TOOLS.mkdir(exist_ok=True)
    archive = TOOLS / asset_name
    partial = archive.with_suffix(archive.suffix + ".partial")
    if not archive.exists():
        print(f"Downloading official Lean {VERSION} to {TOOLS} ({asset['size'] // 1_000_000} MB).", flush=True)
        subprocess.run(
            ["curl", "--silent", "--show-error", "--fail", "--location", "--retry", "2", "--output", str(partial), url],
            check=True,
        )
        partial.replace(archive)
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        archive.unlink()
        raise RuntimeError("Downloaded Lean archive failed its official SHA-256 check.")
    print("SHA-256 verified; extracting project-local toolchain.", flush=True)
    with tempfile.TemporaryDirectory(prefix="lean-install-", dir=TOOLS) as stage:
        subprocess.run(["tar", "-xf", str(archive), "-C", stage], check=True)
        roots = [p for p in Path(stage).iterdir() if p.is_dir() and (p / "bin" / "lean").exists()]
        if len(roots) != 1:
            entries = [str(p.name) for p in Path(stage).iterdir()]
            raise RuntimeError(f"Unexpected official archive layout: {entries}")
        if DEST.exists():
            raise RuntimeError(f"Refusing to overwrite existing {DEST}")
        shutil.move(str(roots[0]), DEST)
    archive.unlink()
    metadata = {"version": VERSION, "asset": asset_name, "sha256": expected, "url": url}
    (TOOLS / "toolchain.json").write_text(json.dumps(metadata, indent=2) + "\n")
    subprocess.run([str(existing), "--version"], check=True)
    print("Toolchain installed. Shell profiles and global tools were not changed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        raise SystemExit(2)
