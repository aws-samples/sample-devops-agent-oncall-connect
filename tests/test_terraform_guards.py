# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live-call guards in modules/oncall, proven with `terraform test` (mock providers, plan only).

- dry_run = false needs enable_pin = true unless allow_live_without_pin = true.
- enable_pin = true refuses enable_flow_logging = true.

Runs offline on a temp copy of the module, with the providers already installed by
`terraform -chdir=terraform/envs/demo init -backend=false`. Skipped when Terraform or those
providers are not available (run the demo init first).
"""

import pathlib
import shutil
import subprocess  # nosec B404 - runs the terraform binary with a fixed argv, no shell
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEMO = ROOT / "terraform" / "envs" / "demo"
PROVIDERS = DEMO / ".terraform" / "providers"
TERRAFORM = shutil.which("terraform")

pytestmark = pytest.mark.skipif(
    TERRAFORM is None or not PROVIDERS.is_dir(),
    reason="needs terraform and the demo providers (terraform -chdir=terraform/envs/demo init -backend=false)",
)


def test_live_call_guards(tmp_path):
    src_root = tmp_path / "srcroot"
    (src_root / "build").mkdir(parents=True)
    (src_root / "src").symlink_to(ROOT / "src")
    with zipfile.ZipFile(src_root / "build" / "layer_boto3.zip", "w") as z:
        z.writestr("placeholder", "x")
    module = tmp_path / "oncall"
    shutil.copytree(ROOT / "terraform" / "modules" / "oncall", module, ignore=shutil.ignore_patterns(".terraform*"))
    shutil.copy(DEMO / ".terraform.lock.hcl", module / ".terraform.lock.hcl")
    (module / "tests").mkdir()
    test_file = (ROOT / "tests" / "fixtures" / "oncall_guards.tftest.hcl").read_text().replace("SRC_ROOT", str(src_root))
    (module / "tests" / "guards.tftest.hcl").write_text(test_file)

    def tf(*args):
        # Fixed argv (no shell): the resolved terraform binary on a temp copy of this repo's module.
        # nosemgrep: dangerous-subprocess-use-audit
        return subprocess.run([TERRAFORM, f"-chdir={module}", *args], capture_output=True, text=True, timeout=600)  # noqa: S603  # nosec B603

    init = tf("init", "-backend=false", "-input=false", f"-plugin-dir={PROVIDERS}")
    assert init.returncode == 0, init.stderr
    out = tf("test", "-no-color")
    assert out.returncode == 0, out.stdout + out.stderr
    for run in (
        "live_without_pin_refused",
        "live_with_override_ok",
        "live_with_pin_ok",
        "pin_with_flow_logging_refused",
        "dry_run_default_ok",
        "replay_bus_ok",
    ):
        assert f'run "{run}"... pass' in out.stdout, out.stdout
