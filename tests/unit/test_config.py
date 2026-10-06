"""Unit tests for common/config.py — the shared SSM configuration loader —
and for stacks/foundation_stack.py — the CMK/S3/SSM foundation stack.

The foundation-stack tests synthesize the CDK app in-process via
`aws_cdk.assertions.Template.from_stack` -- no `cdk synth` CLI, no AWS
credentials, no deployment -- and assert on the resulting CloudFormation
template. They live here (rather than in a separate test module) because
`tests/unit/test_config.py` is the single test file this module owns.
"""
import os

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
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
from stacks.foundation_stack import FoundationStack

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


# ---------------------------------------------------------------------------
# stacks/foundation_stack.py — CDK synth assertions
# ---------------------------------------------------------------------------
def _synth_template(**overrides) -> Template:
    app = cdk.App()
    stack = FoundationStack(
        app,
        "TestFoundationStack",
        env_name="test",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
        **overrides,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return _synth_template()


def test_kms_key_is_customer_managed_with_rotation(template):
    template.has_resource_properties(
        "AWS::KMS::Key",
        {
            "EnableKeyRotation": True,
        },
    )


def test_kms_key_policy_does_not_grant_wildcard_principal_encryption_actions(template):
    resources = template.find_resources("AWS::KMS::Key")
    assert resources, "expected at least one AWS::KMS::Key resource"

    sensitive_actions = {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:GenerateDataKey"}

    for key_id, key_def in resources.items():
        statements = key_def["Properties"]["KeyPolicy"]["Statement"]
        for statement in statements:
            actions = statement["Action"]
            if isinstance(actions, str):
                actions = [actions]
            if sensitive_actions.intersection(actions):
                principal = statement.get("Principal", {})
                # Must not be AWS: "*" (a bare wildcard principal). Root
                # account management statements use the account root ARN,
                # which is fine (not "*"); named-role grants reference
                # the role ARN. Neither case is a bare "*".
                aws_principal = principal.get("AWS") if isinstance(principal, dict) else None
                if isinstance(aws_principal, str):
                    assert aws_principal != "*", (
                        f"statement {statement} in key {key_id} grants a sensitive "
                        "KMS action to a bare '*' principal"
                    )


def test_kms_key_policy_grants_named_roles_when_provided():
    # Roles for the KVS stream / inference Lambda / stream-configurator are
    # owned by their own stacks in the real app, so the foundation stack
    # receives their ARNs (strings) rather than live cross-stack
    # constructs -- passing live constructs here would create a circular
    # CloudFormation stack dependency, since those stacks already depend
    # on this one for the CMK's ARN.
    app = cdk.App()
    kvs_role_arn = "arn:aws:iam::123456789012:role/KvsStreamRole"
    inference_role_arn = "arn:aws:iam::123456789012:role/InferenceLambdaRole"
    configurator_role_arn = "arn:aws:iam::123456789012:role/StreamConfiguratorRole"

    stack = FoundationStack(
        app,
        "FoundationWithRoles",
        env_name="test",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
        kvs_role_arn=kvs_role_arn,
        inference_lambda_role_arn=inference_role_arn,
        stream_configurator_role_arn=configurator_role_arn,
    )
    template = Template.from_stack(stack)

    key_resources = template.find_resources("AWS::KMS::Key")
    assert len(key_resources) == 1
    key_id, key_def = next(iter(key_resources.items()))
    statements = key_def["Properties"]["KeyPolicy"]["Statement"]

    named_arns = {kvs_role_arn, inference_role_arn, configurator_role_arn}

    # Collect every statement whose principal is one of our three named
    # role ARNs (never '*').
    role_grant_statements = [
        s
        for s in statements
        if isinstance(s.get("Principal", {}).get("AWS"), str)
        and s["Principal"]["AWS"] in named_arns
    ]
    assert len(role_grant_statements) == 3, (
        f"expected 3 key-policy statements naming the KVS/inference/"
        f"stream-configurator role ARNs, found {len(role_grant_statements)}: {statements}"
    )

    granted_actions = set()
    for statement in role_grant_statements:
        actions = statement["Action"]
        if isinstance(actions, str):
            actions = [actions]
        granted_actions.update(actions)
        assert statement["Principal"]["AWS"] != "*"

    assert "kms:Decrypt" in granted_actions
    assert any(a.startswith("kms:GenerateDataKey") for a in granted_actions)
    # Encrypt is only granted to the KVS role (include_encrypt=True), but
    # at least one statement overall should carry it.
    assert "kms:Encrypt" in granted_actions


def test_bucket_encryption_references_cmk_by_ref_not_literal_arn(template):
    buckets = template.find_resources("AWS::S3::Bucket")
    assert buckets, "expected at least one AWS::S3::Bucket resource"

    for bucket_id, bucket_def in buckets.items():
        encryption = bucket_def["Properties"].get("BucketEncryption")
        assert encryption is not None, f"bucket {bucket_id} is missing BucketEncryption"

        rules = encryption["ServerSideEncryptionConfiguration"]
        assert rules, f"bucket {bucket_id} has no SSE rules"

        sse_default = rules[0]["ServerSideEncryptionByDefault"]
        assert sse_default["SSEAlgorithm"] == "aws:kms"

        kms_key_id = sse_default["KMSMasterKeyID"]
        # Must be a CFN intrinsic (Ref/Fn::GetAtt) pointing at the key
        # construct -- never a hard-coded ARN string.
        assert isinstance(kms_key_id, dict), (
            f"bucket {bucket_id} BucketEncryption.KMSMasterKeyID is a literal "
            f"value ({kms_key_id!r}), expected a Ref/Fn::GetAtt to the CMK"
        )
        assert "Ref" in kms_key_id or "Fn::GetAtt" in kms_key_id


def test_no_bucket_omits_bucket_encryption(template):
    buckets = template.find_resources("AWS::S3::Bucket")
    assert buckets
    for bucket_id, bucket_def in buckets.items():
        assert "BucketEncryption" in bucket_def["Properties"], (
            f"bucket {bucket_id} has no BucketEncryption configured"
        )


def test_bucket_policy_denies_non_tls_requests(template):
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {
                                    "Bool": {"aws:SecureTransport": "false"}
                                },
                            }
                        )
                    ]
                )
            }
        },
    )


def test_bucket_blocks_public_access(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )


@pytest.mark.parametrize(
    "leaf_name",
    [
        "bucket-name",
        "stream-name",
        "sagemaker-endpoint-name",
        "confidence-threshold",
        "sns-topic-arn",
        "region",
        "cmk-key-arn",
    ],
)
def test_ssm_parameter_exists_under_prefix(template, leaf_name):
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": f"/fire-detection/test/{leaf_name}",
            "Type": "String",
        },
    )


def test_confidence_threshold_default_is_0_75(template):
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": "/fire-detection/test/confidence-threshold",
            "Value": "0.75",
        },
    )


def test_ssm_parameters_are_independently_defined_resources(template):
    # Each SSM parameter must be its own resource (independently updatable
    # without touching/redeploying Lambda code), not a single blob.
    params = template.find_resources("AWS::SSM::Parameter")
    assert len(params) >= 7
