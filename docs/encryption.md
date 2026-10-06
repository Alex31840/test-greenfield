# Encryption

This pipeline uses a single customer-managed KMS key (CMK) — created in
`stacks/foundation_stack.py` as `FoundationStack.cmk`, aliased
`alias/fire-detection-{env}` — shared by every component that needs to
read or write encrypted data: the S3 image bucket, the KVS stream's
image-generation destination, the inference Lambda, and (per the SNS
stack, when added) the SNS topic. CMKs enforce their own **key policy**
in addition to whatever IAM policy a caller's role carries — an IAM
`Allow` is not sufficient on its own; the key policy must also name the
caller. This document describes exactly which component needs which
action against this CMK and why, so that as the KVS/inference/SNS
stacks are completed, each can be wired in with least privilege.

## Why both an IAM grant and a key-policy statement

`stacks/foundation_stack.py` grants CMK usage through
`FoundationStack._grant_cmk_usage()`, which is called once per
component role:

| Component role | Passed as | `include_encrypt` |
|---|---|---|
| KVS stream role | `kvs_role` (live construct) or `kvs_role_arn` (string) | `True` |
| Inference Lambda role | `inference_lambda_role` / `inference_lambda_role_arn` | `False` |
| Stream-configurator Lambda role | `stream_configurator_role` / `stream_configurator_role_arn` | `False` |

For each, two things happen:

1. **IAM side** (only possible when a live, same-stack construct is
   passed): `kms.Key.grant_decrypt()` / `grant_encrypt_decrypt()` /
   `grant()` add a statement to the *grantee role's own* IAM policy,
   scoped to this CMK's ARN.
2. **Key-policy side** (always, regardless of whether a live construct
   or a plain ARN string was passed): an explicit `Allow` statement
   naming that role's ARN as `Principal` is added directly to the CMK's
   own key-policy document via `add_to_resource_policy`. This is what
   satisfies the CMK's own enforcement — and keeps the key policy
   itself inspectable (via `cdk synth` / `Template.from_stack`) as proof
   that only these three named roles, never `"*"`, can use the key.

**Why an ARN string instead of a live construct is the normal case
here:** the KVS stream role, the inference Lambda's execution role, and
the stream-configurator Lambda's execution role are each owned by their
own stack (`kvs_stack.py`, `inference_stack.py`), not by
`foundation_stack.py`. If `foundation_stack.py` referenced those other
stacks' role *constructs* directly, CDK would need
`FoundationStack -> {Kvs,Inference}Stack` for the role resource at the
same time those stacks already depend on `FoundationStack -> {other
stack}` for the CMK's ARN — a circular CloudFormation stack dependency,
which CDK synth rejects outright (`DependencyCycle` error, confirmed
when prototyping this stack with a live cross-stack `iam.Role`
construct). Passing the role's ARN as a plain string breaks the cycle:
the key policy can name an ARN it has no live CDK reference to, and the
owning stack is still free to depend on `FoundationStack` for the key
ARN it needs to attach to its own role's IAM policy.

## Per-component requirement detail

### KVS stream role

* **Needs:** `kms:Decrypt`, `kms:GenerateDataKey*`, and `kms:Encrypt`
  against this CMK.
* **Why:** KVS's image-generation feature writes extracted JPEG frames
  directly to the S3 destination bucket on the stream owner's behalf.
  Since the bucket's default encryption is `aws:kms` using this CMK,
  every `PutObject` KVS performs needs `kms:GenerateDataKey*` (to create
  the per-object data key) and `kms:Encrypt`; subsequent reads of
  existing encrypted objects (e.g. `DescribeImageGenerationConfiguration`
  round-trips, or any read-before-write check) need `kms:Decrypt`. The
  KVS developer guide's own walkthrough for encrypted S3 destinations
  lists the role as needing `kms:Decrypt` and `s3:PutObject` as a
  minimum (`.docs/kinesisvideo_dg.txt`, page 245, "How do I get started
  with server-side encryption?"); this stack also grants `kms:Encrypt`
  and `kms:GenerateDataKey*` explicitly, since `PutObject` against an
  SSE-KMS bucket requires generating a fresh data key for each object,
  not merely decrypting an existing one.
* **Also needs (not part of this stack):** `s3:PutObject` on the image
  bucket itself — an S3-level IAM/bucket-policy grant, independent of
  the KMS key policy, owned by whichever stack defines the KVS role
  (`kvs_stack.py`).

### Inference Lambda role

* **Needs:** `kms:Decrypt`, `kms:GenerateDataKey*` against this CMK.
  (No `kms:Encrypt` — see below.)
* **Why:** the inference-trigger Lambda is invoked by an S3
  `ObjectCreated` event and performs `GetObject`/`HeadObject` against the
  (SSE-KMS-encrypted) image bucket to read the frame KVS just wrote, so
  it needs `kms:Decrypt` to unwrap the object's data key. It does not
  itself write new objects into the CMK-encrypted bucket, so it is not
  granted `kms:Encrypt`. `kms:GenerateDataKey*` is still granted because
  some SDK code paths (e.g. multipart `GetObject` range reads in some
  boto3/S3 configurations) request a fresh grant during decrypt-adjacent
  calls; granting it avoids an avoidable `AccessDenied` without widening
  the action to `kms:Encrypt`.
* **Also needs (not part of this stack):** read access to the
  SSM parameters under `/fire-detection/{env}/...` (see
  `docs/configuration.md`), `sagemaker:InvokeEndpoint` on the configured
  endpoint, and `sns:Publish` on the configured topic — owned by
  `inference_stack.py`.

### Stream-configurator Lambda role (CloudFormation custom resource)

* **Needs:** `kms:Decrypt`, `kms:GenerateDataKey*` against this CMK.
* **Why:** this Lambda calls KVS's
  `UpdateImageGenerationConfiguration` (and polls
  `DescribeImageGenerationConfiguration`) during stack create/update, and
  may need to validate the destination bucket/CMK wiring is correct
  (e.g. a `HeadObject`/`GetBucketEncryption` check) as part of its
  custom-resource success/failure signalling. It does not write objects
  into the bucket itself, so `kms:Encrypt` is not granted.
* **Also needs (not part of this stack):**
  `kinesisvideo:UpdateImageGenerationConfiguration`,
  `kinesisvideo:DescribeImageGenerationConfiguration`,
  `kinesisvideo:GetDataEndpoint` on the configured stream — owned by
  `kvs_stack.py`.

## S3 bucket encryption

* `stacks/foundation_stack.py`'s `ImageBucket` sets
  `encryption=aws_s3.BucketEncryption.KMS` with `encryption_key=self.cmk`
  (referenced by CDK `Ref`/`Fn::GetAtt`, never a literal key ARN string —
  this is asserted in `tests/unit/test_config.py::test_bucket_encryption_references_cmk_by_ref_not_literal_arn`)
  and `bucket_key_enabled=True` (S3 Bucket Keys, to reduce the volume of
  KMS API calls/cost at the per-object sampling rate KVS image
  generation produces).
* `enforce_ssl=True` plus an explicit `Deny` statement
  (`Condition: {"Bool": {"aws:SecureTransport": "false"}}`) on the bucket
  policy reject any non-TLS request to the bucket or its objects,
  regardless of which principal makes it.
* `block_public_access=s3.BlockPublicAccess.BLOCK_ALL` — the image
  bucket is never public; every reader must be a named IAM principal
  with both an S3 IAM/bucket-policy grant and a KMS key-policy grant.

## KMS key administration

* The CMK's base key policy grants the account root full `kms:*`
  administrative access (required so the key always remains manageable
  by whoever can administer the AWS account — this is the default CDK
  `kms.Key` behavior and is not itself a usage grant to any workload
  role).
* `enable_key_rotation=True` — automatic annual key rotation is enabled;
  this does not require any coordination from the components above,
  since KMS handles re-wrapping of data keys transparently.
* `removal_policy=RemovalPolicy.RETAIN` on both the CMK and the S3
  bucket — neither is deleted by a stack deletion/rollback, to avoid
  data loss of already-encrypted images or orphaning ciphertext that
  can no longer be decrypted.

## SNS topic encryption (forward reference)

The SNS topic itself is owned by `sns_stack.py` (not this stack). Per
the per-component least-privilege principle this document documents for
S3/KVS/inference, when `sns_stack.py` is implemented its topic should
either reuse this same CMK (granting `sns_role`/publisher roles
`kms:GenerateDataKey*`/`kms:Decrypt` against it the same way as above)
or use its own CMK with an equivalent key-policy pattern — either way,
no SNS-related role should be granted key-policy access via a wildcard
principal.
