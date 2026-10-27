# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Builds the two artifacts Terraform deploys (run before `terraform apply`).

- build/layer_boto3.zip         : recent boto3 for Lambda (the runtime one may not know devops-agent)
- build/mcp_server.zip          : MCP server + src/common/sanitize.py + dependencies for AgentCore Runtime
                                  (Linux arm64, Python 3.13)

Dependencies are locked with hashes in src/**/requirements.txt (regenerate from requirements.in
with `uv pip compile --generate-hashes`, see README).

Usage: python3 scripts/build.py
"""

import pathlib
import shutil
import subprocess  # nosec B404 - fixed argument lists built from constants, no shell
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
# uv if available (fast, no pip needed in the venv), pip otherwise.
if shutil.which("uv"):
    PIP = ["uv", "pip", "install", "--quiet", "--require-hashes", "--python", sys.executable]
    LINUX_ARM64 = ["--python-platform", "aarch64-manylinux2014", "--python-version", "3.13", "--only-binary", ":all:"]
else:
    PIP = [sys.executable, "-m", "pip", "install", "--quiet", "--require-hashes", "--upgrade", "--no-compile"]
    LINUX_ARM64 = ["--platform", "manylinux2014_aarch64", "--implementation", "cp", "--python-version", "3.13", "--only-binary=:all:"]


def _zip(src: pathlib.Path, out: pathlib.Path) -> None:
    """Zip a directory with fixed timestamps: same inputs -> same zip -> no spurious Terraform diff."""
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                info = zipfile.ZipInfo(str(f.relative_to(src)), date_time=(2020, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, f.read_bytes())


def build_layer() -> None:
    root = BUILD / "layer_boto3"
    shutil.rmtree(root, ignore_errors=True)
    # Fixed argv list (no shell): pip on this repo's hash-locked requirements file.
    # nosemgrep: dangerous-subprocess-use-audit
    subprocess.run([*PIP, "-r", str(ROOT / "src/layers/boto3/requirements.txt"), "--target", str(root / "python")], check=True)  # nosec B603
    out = BUILD / "layer_boto3.zip"
    _zip(root, out)
    print(f"layer : {out} ({out.stat().st_size // 1024} KiB)")


def build_mcp() -> None:
    pkg = BUILD / "mcp_pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    # Fixed argv list (no shell): pip on this repo's hash-locked requirements file.
    # nosemgrep: dangerous-subprocess-use-audit
    subprocess.run([*PIP, *LINUX_ARM64, "-r", str(ROOT / "src/mcp_server/requirements.txt"), "--target", str(pkg)], check=True)  # nosec B603
    shutil.copy(ROOT / "src/mcp_server/server.py", pkg / "server.py")
    # The ONE shared untrusted-text sanitizer (the Lambdas get the same file through archive_file).
    shutil.copy(ROOT / "src/common/sanitize.py", pkg / "sanitize.py")
    out = BUILD / "mcp_server.zip"
    _zip(pkg, out)
    print(f"mcp   : {out} ({out.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    BUILD.mkdir(exist_ok=True)
    build_layer()
    build_mcp()
