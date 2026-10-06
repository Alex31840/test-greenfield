#!/usr/bin/env python3
"""CDK App entry point.

This is the single source of infrastructure truth for the fire-detection
pipeline: every stack (foundation / KVS / inference / SNS / reliability)
is instantiated here, from one `App`, and synthesized to one
CloudFormation template set per deployment.

Only `FoundationStack` is wired in by this objective (the CMK, the
encrypted S3 image bucket, and the SSM configuration store). The other
stacks (`KvsStack`, `InferenceStack`, `SnsStack`, `ReliabilityStack`) are
owned by their respective objectives; once present under `stacks/`, they
are instantiated here too, wired to `foundation_stack`'s outputs (bucket,
CMK, SSM parameter prefix) the same way `FoundationStack` is below.
"""
from __future__ import annotations

import os

import aws_cdk as cdk

from stacks.foundation_stack import FoundationStack

app = cdk.App()

env_name = app.node.try_get_context("fire-detection:env") or os.environ.get(
    "FIRE_DETECTION_ENV", "dev"
)

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

foundation_stack = FoundationStack(
    app,
    f"FoundationStack-{env_name}",
    env_name=env_name,
    env=env,
    description=(
        "Foundation resources for the fire-detection pipeline: "
        "customer-managed KMS key, encrypted S3 image bucket, and the "
        "SSM Parameter Store configuration every other stack/Lambda "
        "reads at runtime."
    ),
)

# Remaining stacks (kvs_stack.KvsStack, inference_stack.InferenceStack,
# sns_stack.SnsStack, reliability_stack.ReliabilityStack) are added here
# by their owning objectives, e.g.:
#
#   kvs_stack = KvsStack(app, f"KvsStack-{env_name}", env_name=env_name, env=env)
#   inference_stack = InferenceStack(
#       app, f"InferenceStack-{env_name}",
#       image_bucket=foundation_stack.image_bucket,
#       cmk=foundation_stack.cmk,
#       env_name=env_name, env=env,
#   )

app.synth()
