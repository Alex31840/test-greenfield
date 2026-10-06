"""Foundation stack: CMK, encrypted S3 image bucket, and the externalized
SSM Parameter Store configuration that every other stack/Lambda reads at
runtime.

Resources created here:

* A customer-managed KMS key (CMK) used to encrypt the S3 image bucket
  (and shared, via key-policy grants, with the KVS stream role, the
  inference Lambda role, and the stream-configurator custom-resource
  Lambda role so each can Encrypt/Decrypt/GenerateDataKey* against it).
* An S3 bucket for extracted KVS images: default encryption
  `aws:kms` using the CMK above, TLS-only bucket policy (`aws:SecureTransport`
  deny), and versioning enabled for recovery.
* SSM `StringParameter` resources under `/fire-detection/{env}/...`
  holding every runtime-resolved identifier other components need:
  bucket name, stream name, SageMaker endpoint name/ARN, confidence
  threshold, SNS topic ARN, region, and the CMK key ARN. Because these
  are independent SSM parameters (not CloudFormation-exported constants
  baked into Lambda env vars), the bucket/stream/endpoint/threshold can
  all be changed by updating a parameter value, with no Lambda
  redeployment required.
"""
from __future__ import annotations

from typing import Optional

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from constructs import Construct

from common.config import (
    BUCKET_NAME,
    CMK_KEY_ARN,
    CONFIDENCE_THRESHOLD,
    REGION,
    SAGEMAKER_ENDPOINT_ARN,
    SAGEMAKER_ENDPOINT_NAME,
    SNS_TOPIC_ARN,
    STREAM_NAME,
    get_parameter_prefix,
)

DEFAULT_CONFIDENCE_THRESHOLD = "0.75"


class FoundationStack(Stack):
    """Provisions the CMK, image bucket, and SSM configuration store.

    The IAM roles for the KVS stream, the inference Lambda, and the
    stream-configurator Lambda are owned by their respective stacks
    (kvs_stack / inference_stack), but this stack is where the CMK key
    policy grant for each of those roles is attached, since the CMK is
    the shared resource and CMKs enforce key-policy grants in addition
    to IAM permissions. Callers pass in the (possibly not-yet-fully-
    configured) IAM roles/principals that need access; this stack grants
    them kms:Decrypt / kms:GenerateDataKey* (and kms:Encrypt where
    applicable) against this specific key only -- never '*'.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str = "dev",
        bucket_name: Optional[str] = None,
        stream_name: str = "drone-fire-stream",
        sagemaker_endpoint_name: str = "fire-detection-endpoint",
        sagemaker_endpoint_arn: Optional[str] = None,
        confidence_threshold: str = DEFAULT_CONFIDENCE_THRESHOLD,
        sns_topic_arn: Optional[str] = None,
        kvs_role: Optional[iam.IGrantable] = None,
        inference_lambda_role: Optional[iam.IGrantable] = None,
        stream_configurator_role: Optional[iam.IGrantable] = None,
        kvs_role_arn: Optional[str] = None,
        inference_lambda_role_arn: Optional[str] = None,
        stream_configurator_role_arn: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.env_name = env_name
        self.param_prefix = get_parameter_prefix(env_name)

        # ------------------------------------------------------------------
        # Customer-managed KMS key
        # ------------------------------------------------------------------
        # Base key policy: root account retains full administrative control
        # (required so the key is always manageable), encryption actions are
        # NOT granted to '*' -- only to the specific named principals passed
        # in below, each scoped to exactly the actions it needs.
        self.cmk = kms.Key(
            self,
            "ImageBucketCmk",
            description=(
                "Customer-managed key for the fire-detection pipeline: "
                "encrypts the S3 image bucket and is shared, via explicit "
                "key-policy grants, with the KVS stream role, the "
                "inference Lambda role, and the stream-configurator role."
            ),
            alias=f"alias/fire-detection-{env_name}",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Grant exactly the named roles the actions they need -- never '*'.
        #
        # Two ways to name a grantee are accepted:
        #   * a live `iam.IGrantable` construct (same stack as this one) --
        #     `grant()` adds the statement to the *grantee's* IAM policy,
        #     scoped to this key's ARN.
        #   * a role ARN string (the common case: the KVS/inference/
        #     stream-configurator roles are owned by their own stacks) --
        #     this is added as an explicit `Principal` statement directly
        #     in the CMK's own key-policy document via
        #     `add_to_resource_policy`. Using the ARN string (rather than
        #     the live cross-stack construct) avoids creating a circular
        #     CloudFormation stack dependency (the other stack already
        #     depends on this stack for the key ARN; this stack must not
        #     turn around and depend on the other stack's role resource).
        self._grant_cmk_usage(
            kvs_role, kvs_role_arn, "kvs-stream-role", include_encrypt=True
        )
        self._grant_cmk_usage(
            inference_lambda_role,
            inference_lambda_role_arn,
            "inference-lambda-role",
            include_encrypt=False,
        )
        self._grant_cmk_usage(
            stream_configurator_role,
            stream_configurator_role_arn,
            "stream-configurator-role",
            include_encrypt=False,
        )

        # ------------------------------------------------------------------
        # S3 image bucket
        # ------------------------------------------------------------------
        # NOTE on key naming: the S3 *object key prefix* under which KVS
        # writes extracted images should use a randomized (not sequential,
        # not purely time-ordered) prefix component -- e.g.
        # s3://<bucket>/<random-hex>/drone-fire-stream/ -- to spread writes
        # across S3's internal partitioning space and avoid a hot-partition
        # bottleneck at high frame-extraction rates. See
        # docs/configuration.md "Destination URI" section for the exact
        # convention; the bucket itself places no restriction on this, it
        # is a convention enforced by whoever configures
        # UpdateImageGenerationConfiguration (the kvs_stack).
        self.image_bucket = s3.Bucket(
            self,
            "ImageBucket",
            bucket_name=bucket_name,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.cmk,
            bucket_key_enabled=True,
            enforce_ssl=True,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="abort-incomplete-multipart-uploads",
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    enabled=True,
                )
            ],
        )

        # `enforce_ssl=True` already adds a Deny statement on
        # aws:SecureTransport=false for all principals/actions on the
        # bucket and its objects; make the intent explicit/self-documenting
        # regardless of the CDK default so this stays true even if the
        # construct's default behaviour ever changes.
        self.image_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyUnencryptedTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[
                    self.image_bucket.bucket_arn,
                    self.image_bucket.arn_for_objects("*"),
                ],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

        # ------------------------------------------------------------------
        # SSM Parameter Store configuration
        # ------------------------------------------------------------------
        resolved_sns_topic_arn = sns_topic_arn or (
            f"arn:aws:sns:{self.region}:{self.account}:fire-detection-{env_name}-alerts"
        )
        resolved_sagemaker_endpoint_arn = sagemaker_endpoint_arn or (
            f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/"
            f"{sagemaker_endpoint_name}"
        )

        self.parameters = {
            BUCKET_NAME: self._string_parameter(
                BUCKET_NAME,
                self.image_bucket.bucket_name,
                "Name of the S3 bucket that receives extracted KVS images.",
            ),
            STREAM_NAME: self._string_parameter(
                STREAM_NAME,
                stream_name,
                "Name of the Kinesis Video Stream producing frames.",
            ),
            SAGEMAKER_ENDPOINT_NAME: self._string_parameter(
                SAGEMAKER_ENDPOINT_NAME,
                sagemaker_endpoint_name,
                "Name of the real-time SageMaker inference endpoint.",
            ),
            SAGEMAKER_ENDPOINT_ARN: self._string_parameter(
                SAGEMAKER_ENDPOINT_ARN,
                resolved_sagemaker_endpoint_arn,
                "ARN of the real-time SageMaker inference endpoint.",
            ),
            CONFIDENCE_THRESHOLD: self._string_parameter(
                CONFIDENCE_THRESHOLD,
                confidence_threshold,
                "Minimum model confidence (0.0-1.0) required to raise a fire alert.",
            ),
            SNS_TOPIC_ARN: self._string_parameter(
                SNS_TOPIC_ARN,
                resolved_sns_topic_arn,
                "ARN of the SNS topic fire alerts are published to.",
            ),
            REGION: self._string_parameter(
                REGION,
                self.region,
                "AWS region the pipeline resources are deployed in.",
            ),
            CMK_KEY_ARN: self._string_parameter(
                CMK_KEY_ARN,
                self.cmk.key_arn,
                "ARN of the customer-managed KMS key shared by this pipeline.",
            ),
        }

    # ----------------------------------------------------------------------
    def _grant_cmk_usage(
        self,
        grantee: Optional[iam.IGrantable],
        grantee_role_arn: Optional[str],
        label: str,
        *,
        include_encrypt: bool,
    ) -> None:
        """Grant a single named principal exactly the KMS actions it needs
        against this CMK -- kms:Decrypt/kms:GenerateDataKey* always,
        kms:Encrypt only where the caller actually writes ciphertext (e.g.
        the KVS stream role writing images into the S3 bucket).

        Accepts either a live same-stack `grantee` construct, or a
        `grantee_role_arn` string for a role owned by another stack (the
        common case here). Exactly one of the two should be provided.

        * For a live construct: `grant()` adds the permission to the
          *grantee's* IAM policy, scoped to this key's ARN, AND an
          explicit statement is added to this CMK's own key-policy
          document naming that role's principal.
        * For an ARN string: only the explicit key-policy statement is
          added (naming the ARN as `Principal`) -- there is no live
          construct here to attach an IAM policy to, and attempting to
          reference the other stack's role construct directly would
          create a circular stack dependency, since that stack already
          depends on this one for the key ARN.

        Either way, the CMK's own key-policy document ends up naming
        exactly this role ARN -- never '*' -- for the sensitive actions,
        which is inspectable via `cdk synth` / `Template.from_stack`.
        """
        if grantee is None and grantee_role_arn is None:
            return

        actions = ["kms:Decrypt", "kms:GenerateDataKey*"]
        if include_encrypt:
            actions.insert(0, "kms:Encrypt")

        if grantee is not None:
            if include_encrypt:
                self.cmk.grant_encrypt_decrypt(grantee)
            else:
                self.cmk.grant_decrypt(grantee)
            self.cmk.grant(grantee, "kms:GenerateDataKey*")
            principal = grantee.grant_principal
        else:
            principal = iam.ArnPrincipal(grantee_role_arn)

        self.cmk.add_to_resource_policy(
            iam.PolicyStatement(
                sid=f"Allow{self._pascal(label)}KeyUsage",
                effect=iam.Effect.ALLOW,
                principals=[principal],
                actions=actions,
                resources=["*"],  # key policies are self-referential: "*" here means "this key"
            )
        )

    def _string_parameter(
        self, leaf_name: str, value: str, description: str
    ) -> ssm.StringParameter:
        return ssm.StringParameter(
            self,
            f"Param{self._pascal(leaf_name)}",
            parameter_name=f"{self.param_prefix}/{leaf_name}",
            string_value=value,
            description=description,
            tier=ssm.ParameterTier.STANDARD,
        )

    @staticmethod
    def _pascal(dash_name: str) -> str:
        return "".join(part.capitalize() for part in dash_name.split("-"))
