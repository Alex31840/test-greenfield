"""Unit tests for stacks/foundation_stack.py.

These tests synthesize the CDK app in-process via
`aws_cdk.assertions.Template.from_stack` -- no `cdk synth` CLI, no AWS
credentials, no deployment -- and assert on the resulting CloudFormation
template.
"""
import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
import pytest

from stacks.foundation_stack import FoundationStack


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


# ---------------------------------------------------------------------------
# KMS key + key policy
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# S3 bucket
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# SSM configuration store
# ---------------------------------------------------------------------------
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
