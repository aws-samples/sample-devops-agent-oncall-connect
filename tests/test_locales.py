# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every locale file must define the same keys, and the flows must only use defined messages."""

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCALES = {p.stem: json.loads(p.read_text()) for p in (ROOT / "src" / "locales").glob("*.json")}


def keys(obj, prefix=""):
    out = set()
    for k, v in obj.items():
        out |= keys(v, f"{prefix}{k}.") if isinstance(v, dict) else {prefix + k}
    return out


def test_locales_exist():
    assert {"en-US", "fr-FR"} <= set(LOCALES)


def test_locales_share_the_same_keys():
    reference = keys(LOCALES["en-US"])
    for name, locale in LOCALES.items():
        assert keys(locale) == reference, name


def test_flow_templates_only_use_known_messages():
    known = set(LOCALES["en-US"]["messages"])
    for flow in (ROOT / "src" / "connect_flows").glob("*.json.tftpl"):
        used = set(re.findall(r"messages\.([a-z0-9_]+)", flow.read_text()))
        assert used and used <= known, flow.name
