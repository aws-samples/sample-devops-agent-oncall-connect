# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Demo workload: a meter-reading function that reads its configuration from CONFIG_JSON.

Fault injection = deploy a malformed CONFIG_JSON. The function then raises, the Lambda Errors
metric rises and the alarm fires. The change is traceable in CloudTrail
(UpdateFunctionConfiguration), which is what AWS DevOps Agent should find during its investigation.
"""

import json
import os
import random
from datetime import UTC, datetime


def _load_config() -> dict:
    # No try/except on purpose: an invalid configuration must fail the request.
    return json.loads(os.environ["CONFIG_JSON"])


def handler(event, context):
    config = _load_config()
    meter_id = (event or {}).get("meterId", "meter-000000")
    reading = {
        "meterId": meter_id,
        "index": random.randint(10000, 99999),  # noqa: S311  # nosec B311 (fake demo reading)
        "unit": config.get("unit", "kWh"),
        "readAt": datetime.now(UTC).isoformat(),
    }
    return reading
