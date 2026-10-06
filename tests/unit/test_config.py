"""Unit tests for common/config.py — the shared SSM configuration loader."""
import os

import boto3
import pytest
from moto import mock_aws

from common.config import (
    ALL_PARAMETER_KEYS,
    BUCKET_NAME,
    CMK_KEY_ARN,
    CONFIDENCE_THRESHOLD,
    ConfigError,
    REGION,
    SAGEMAKER_ENDPOINT_ARN,
    SAGEMAKER_ENDPOINT_NAME,
    SNS_TOPIC_ARN,
    STREAM_NAME,
    get_parameter_prefix,
    load_config,
    load_raw_config,
)

REGION_NAME = "us-east-1"


def _put_all_parameters(ssm_client, prefix: str, overrides: dict | None = None) -> dict:
    values = {
        BUCKET_NAME: "fire-detection-dev-images",
        STREAM_NAME: "drone-fire-stream",
        SAGEMAKER_ENDPOINT_NAME: "fire-detection-endpoint",
        SAGEMAKER_ENDPOINT_ARN: "arn:aws:sagemaker:us-east-1:123456789012:endpoint/fire-detection-endpoint",
        CONFIDENCE_THRESHOLD: "0.75",
        SNS_TOPIC_ARN: "arn:aws:sns:us-east-1:123456789012:fire-alerts",
        REGION: REGION_NAME,
        CMK_KEY_ARN: "arn:aws:kms:us-east-1:123456789012:key/abcd-1234",
    }
    if overrides:
        values.update(overrides)
    for leaf, value in values.items():
        ssm_client.put_parameter(
            Name=f"{prefix}/{leaf}",
            Value=value,
            Type="String",
        )
    return values


@pytest.fixture
def ssm_client():
    with mock_aws():
        yield boto3.client("ssm", region_name=REGION_NAME)


def test_get_parameter_prefix_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("FIRE_DETECTION_ENV", raising=False)
    assert get_parameter_prefix() == "/fire-detection/dev"


def test_get_parameter_prefix_honours_env_argument():
    assert get_parameter_prefix("staging") == "/fire-detection/staging"


def test_get_parameter_prefix_honours_environment_variable(monkeypatch):
    monkeypatch.setenv("FIRE_DETECTION_ENV", "prod")
    assert get_parameter_prefix() == "/fire-detection/prod"


def test_load_raw_config_reads_every_parameter_under_prefix(ssm_client):
    prefix = get_parameter_prefix("dev")
    expected = _put_all_parameters(ssm_client, prefix)

    raw = load_raw_config(env="dev", ssm_client=ssm_client, required_keys=ALL_PARAMETER_KEYS)

    for key in ALL_PARAMETER_KEYS:
        assert raw[key] == expected[key]


def test_load_config_returns_typed_app_config(ssm_client):
    prefix = get_parameter_prefix("dev")
    _put_all_parameters(ssm_client, prefix)

    config = load_config(env="dev", ssm_client=ssm_client)

    assert config.bucket_name == "fire-detection-dev-images"
    assert config.stream_name == "drone-fire-stream"
    assert config.sagemaker_endpoint_name == "fire-detection-endpoint"
    assert config.confidence_threshold == pytest.approx(0.75)
    assert config.sns_topic_arn == "arn:aws:sns:us-east-1:123456789012:fire-alerts"
    assert config.region == REGION_NAME
    assert config.cmk_key_arn == "arn:aws:kms:us-east-1:123456789012:key/abcd-1234"
    assert config.sagemaker_endpoint_arn is not None


def test_load_config_raises_clear_exception_when_parameter_missing(ssm_client):
    prefix = get_parameter_prefix("dev")
    _put_all_parameters(ssm_client, prefix)
    # Remove one required parameter to simulate an incomplete configuration.
    ssm_client.delete_parameter(Name=f"{prefix}/{CONFIDENCE_THRESHOLD}")

    with pytest.raises(ConfigError) as excinfo:
        load_config(env="dev", ssm_client=ssm_client)

    assert CONFIDENCE_THRESHOLD in str(excinfo.value)
    assert prefix in str(excinfo.value)


def test_load_config_raises_when_no_parameters_exist_at_all(ssm_client):
    with pytest.raises(ConfigError):
        load_config(env="dev", ssm_client=ssm_client)


def test_load_config_does_not_return_partial_config_on_missing_keys(ssm_client):
    prefix = get_parameter_prefix("dev")
    _put_all_parameters(ssm_client, prefix)
    ssm_client.delete_parameter(Name=f"{prefix}/{BUCKET_NAME}")

    # Must not silently substitute a default/partial config: it must raise.
    with pytest.raises(ConfigError):
        load_config(env="dev", ssm_client=ssm_client)


def test_load_config_different_environments_are_isolated(ssm_client):
    dev_prefix = get_parameter_prefix("dev")
    staging_prefix = get_parameter_prefix("staging")
    _put_all_parameters(ssm_client, dev_prefix, overrides={BUCKET_NAME: "dev-bucket"})
    _put_all_parameters(ssm_client, staging_prefix, overrides={BUCKET_NAME: "staging-bucket"})

    dev_config = load_config(env="dev", ssm_client=ssm_client)
    staging_config = load_config(env="staging", ssm_client=ssm_client)

    assert dev_config.bucket_name == "dev-bucket"
    assert staging_config.bucket_name == "staging-bucket"


def test_load_raw_config_requires_only_requested_keys(ssm_client):
    prefix = get_parameter_prefix("dev")
    # Only put a subset of parameters.
    ssm_client.put_parameter(Name=f"{prefix}/{BUCKET_NAME}", Value="b", Type="String")

    raw = load_raw_config(
        env="dev", ssm_client=ssm_client, required_keys=(BUCKET_NAME,)
    )
    assert raw[BUCKET_NAME] == "b"


def test_load_config_raises_configerror_not_keyerror_for_partial_appconfig(ssm_client):
    prefix = get_parameter_prefix("dev")
    # Only put a subset of parameters, but relax required_keys so
    # load_raw_config succeeds; AppConfig construction itself still needs
    # every AppConfig field, so this must surface as a ConfigError, never
    # a raw KeyError leaking out of this module.
    ssm_client.put_parameter(Name=f"{prefix}/{BUCKET_NAME}", Value="b", Type="String")

    with pytest.raises(ConfigError):
        load_config(env="dev", ssm_client=ssm_client, required_keys=(BUCKET_NAME,))
