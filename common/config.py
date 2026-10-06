"""Shared runtime configuration loader.

Every Lambda in this application (inference-trigger, stream-configurator)
resolves ALL of its resource identifiers — bucket name, stream name,
SageMaker endpoint, confidence threshold, SNS topic ARN, region, CMK key
ARN — from AWS Systems Manager Parameter Store at *runtime*, instead of
having them baked into the deployment package. This module is the single
place that knows the parameter naming convention and how to read it.

Parameter naming convention
----------------------------
All parameters for a given deployment environment live under one path
prefix::

    /fire-detection/{env}/{leaf-name}

where ``{env}`` defaults to ``dev`` and can be overridden with the
``FIRE_DETECTION_ENV`` environment variable (this is how the CDK stack
and the Lambda functions agree on which copy of the parameters to read
without redeploying code when only the parameter *values* change).

The leaf names are fixed and documented in ``docs/configuration.md``:

* ``bucket-name``
* ``stream-name``
* ``sagemaker-endpoint-name``
* ``sagemaker-endpoint-arn``
* ``confidence-threshold``
* ``sns-topic-arn``
* ``region``
* ``cmk-key-arn``
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import boto3

# Leaf names that make up the full set of configuration keys every
# component is expected to be able to read. Individual callers may only
# need a subset (see `required_keys` on `load_config`).
PARAM_PREFIX_TEMPLATE = "/fire-detection/{env}"

BUCKET_NAME = "bucket-name"
STREAM_NAME = "stream-name"
SAGEMAKER_ENDPOINT_NAME = "sagemaker-endpoint-name"
SAGEMAKER_ENDPOINT_ARN = "sagemaker-endpoint-arn"
CONFIDENCE_THRESHOLD = "confidence-threshold"
SNS_TOPIC_ARN = "sns-topic-arn"
REGION = "region"
CMK_KEY_ARN = "cmk-key-arn"

ALL_PARAMETER_KEYS: tuple = (
    BUCKET_NAME,
    STREAM_NAME,
    SAGEMAKER_ENDPOINT_NAME,
    SAGEMAKER_ENDPOINT_ARN,
    CONFIDENCE_THRESHOLD,
    SNS_TOPIC_ARN,
    REGION,
    CMK_KEY_ARN,
)

# The subset of parameters required for core operation. endpoint ARN vs
# endpoint NAME: callers may resolve the SageMaker endpoint by either
# name or ARN, so neither alone is unconditionally required here — the
# default set below only requires the universally-needed keys plus the
# endpoint name (the common case for invoke_endpoint calls).
DEFAULT_REQUIRED_KEYS: tuple = (
    BUCKET_NAME,
    STREAM_NAME,
    SAGEMAKER_ENDPOINT_NAME,
    CONFIDENCE_THRESHOLD,
    SNS_TOPIC_ARN,
    REGION,
    CMK_KEY_ARN,
)


class ConfigError(Exception):
    """Raised when required configuration cannot be resolved from SSM."""


def get_parameter_prefix(env: Optional[str] = None) -> str:
    """Return the SSM path prefix for the given (or ambient) environment."""
    resolved_env = env or os.environ.get("FIRE_DETECTION_ENV", "dev")
    return PARAM_PREFIX_TEMPLATE.format(env=resolved_env)


@dataclass(frozen=True)
class AppConfig:
    """Strongly-typed view over the raw SSM parameter values."""

    bucket_name: str
    stream_name: str
    sagemaker_endpoint_name: str
    confidence_threshold: float
    sns_topic_arn: str
    region: str
    cmk_key_arn: str
    sagemaker_endpoint_arn: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: Dict[str, str]) -> "AppConfig":
        return cls(
            bucket_name=raw[BUCKET_NAME],
            stream_name=raw[STREAM_NAME],
            sagemaker_endpoint_name=raw[SAGEMAKER_ENDPOINT_NAME],
            confidence_threshold=float(raw[CONFIDENCE_THRESHOLD]),
            sns_topic_arn=raw[SNS_TOPIC_ARN],
            region=raw[REGION],
            cmk_key_arn=raw[CMK_KEY_ARN],
            sagemaker_endpoint_arn=raw.get(SAGEMAKER_ENDPOINT_ARN),
        )


def _fetch_parameters_by_path(ssm_client, prefix: str) -> Dict[str, str]:
    """Fetch every parameter under `prefix`, keyed by its leaf name."""
    values: Dict[str, str] = {}
    paginator = ssm_client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(
        Path=prefix,
        Recursive=False,
        WithDecryption=True,
    ):
        for param in page.get("Parameters", []):
            leaf = param["Name"].rsplit("/", 1)[-1]
            values[leaf] = param["Value"]
    return values


def load_raw_config(
    env: Optional[str] = None,
    ssm_client=None,
    required_keys: Iterable[str] = DEFAULT_REQUIRED_KEYS,
) -> Dict[str, str]:
    """Read all SSM parameters under the configured path prefix.

    Raises `ConfigError` (with the list of missing keys) if any key in
    `required_keys` is absent, rather than silently returning a partial
    configuration or substituting defaults.
    """
    prefix = get_parameter_prefix(env)
    client = ssm_client or boto3.client("ssm")

    try:
        values = _fetch_parameters_by_path(client, prefix)
    except Exception as exc:  # noqa: BLE001 - surface any boto3/client error clearly
        raise ConfigError(
            f"Failed to read configuration from SSM Parameter Store under "
            f"'{prefix}': {exc}"
        ) from exc

    missing = [key for key in required_keys if key not in values]
    if missing:
        raise ConfigError(
            "Missing required configuration parameter(s) under "
            f"'{prefix}': {', '.join(sorted(missing))}. "
            "Every required parameter must be created (see docs/configuration.md) "
            "before this component can run."
        )

    return values


def load_config(
    env: Optional[str] = None,
    ssm_client=None,
    required_keys: Iterable[str] = DEFAULT_REQUIRED_KEYS,
) -> AppConfig:
    """Load and validate the full application configuration from SSM.

    This is the function other components (inference-trigger,
    stream-configurator) call at the start of every invocation. It never
    returns a partial config: either every required key is present, or a
    `ConfigError` is raised describing exactly what is missing.
    """
    raw = load_raw_config(env=env, ssm_client=ssm_client, required_keys=required_keys)
    try:
        return AppConfig.from_raw(raw)
    except KeyError as exc:  # pragma: no cover - defensive, caught by missing-check above
        raise ConfigError(f"Missing required configuration key: {exc}") from exc
