# Configuration

Every component in the fire-detection pipeline (the inference-trigger
Lambda, the stream-configurator custom-resource Lambda, and any future
reader) resolves **all** of its resource identifiers — S3 bucket name,
KVS stream name, SageMaker endpoint name/ARN, confidence threshold, SNS
topic ARN, region, CMK key ARN — from **AWS Systems Manager Parameter
Store at runtime**. None of these values are baked into a Lambda
deployment package or hard-coded in application code. This lets an
operator change, e.g., the confidence threshold or swap the SNS topic
without redeploying any Lambda code: just update the parameter value.

`stacks/foundation_stack.py` creates the parameters; `common/config.py`
is the single shared loader every Lambda uses to read them back.

## Parameter naming convention

All parameters for a given deployment environment live under one path
prefix:

```
/fire-detection/{env}/{leaf-name}
```

`{env}` defaults to `dev` and is controlled by:

* the CDK context key `fire-detection:env` (see `cdk.json`), at
  synth/deploy time, and
* the `FIRE_DETECTION_ENV` environment variable, at Lambda runtime (set
  on each function so it reads the same environment's parameters the
  stack created).

Both sides must agree on `{env}` for a deployment to wire together
correctly — this is the only identifier that **is** fixed at deploy
time; everything under the prefix is runtime-resolved.

## Parameter leaves

| Leaf name                   | Example value                                                     | Set by               | Default                | Independently updatable |
|------------------------------|--------------------------------------------------------------------|----------------------|-------------------------|--------------------------|
| `bucket-name`                | `fire-detection-dev-images-ab12cd34`                                | `foundation_stack.py` | — (bucket's generated/explicit name) | yes |
| `stream-name`                | `drone-fire-stream`                                                 | `foundation_stack.py` / `kvs_stack.py` | `drone-fire-stream` | yes |
| `sagemaker-endpoint-name`    | `fire-detection-endpoint`                                           | `foundation_stack.py` / `inference_stack.py` | `fire-detection-endpoint` | yes |
| `sagemaker-endpoint-arn`     | `arn:aws:sagemaker:us-east-1:123456789012:endpoint/fire-detection-endpoint` | `foundation_stack.py` / `inference_stack.py` | derived from endpoint name | yes |
| `confidence-threshold`       | `0.75`                                                              | `foundation_stack.py` | `0.75`                  | yes |
| `sns-topic-arn`              | `arn:aws:sns:us-east-1:123456789012:fire-detection-dev-alerts`      | `foundation_stack.py` / `sns_stack.py` | derived placeholder until `sns_stack` supplies the real ARN | yes |
| `region`                     | `us-east-1`                                                         | `foundation_stack.py` | stack's deploy region   | yes |
| `cmk-key-arn`                | `arn:aws:kms:us-east-1:123456789012:key/...`                        | `foundation_stack.py` | the stack's CMK ARN     | yes |

Each leaf is its own `AWS::SSM::Parameter` resource (`aws_ssm.StringParameter`
in `foundation_stack.py`), not one combined JSON blob — this is what
makes every value independently updatable in-place (via the AWS Console,
CLI `put-parameter`, or an out-of-band CDK/Console change) without ever
touching the Lambda code or its deployment package.

### Changing a value after deployment

To change, e.g., the confidence threshold without redeploying anything:

```bash
aws ssm put-parameter \
  --name /fire-detection/dev/confidence-threshold \
  --value 0.80 \
  --type String \
  --overwrite
```

The next inference-trigger invocation picks up `0.80` on its next call to
`common.config.load_config()` — there is no caching layer in this
module, so the change takes effect immediately (at the cost of one SSM
`GetParametersByPath` call per invocation; see "Cold start / latency
note" below).

To change which bucket/stream/endpoint/topic the pipeline points at,
update the corresponding parameter the same way. Changing `bucket-name`
or `stream-name` does **not** move data or recreate the underlying
resources — it only changes which existing bucket/stream the Lambdas
read/write against on their next invocation, so coordinate such a change
with whatever process keeps the KVS stream's
`UpdateImageGenerationConfiguration` destination URI and the parameter
in sync.

## `common/config.py` contract

```python
from common.config import load_config, ConfigError

config = load_config()          # reads /fire-detection/{FIRE_DETECTION_ENV or "dev"}/...
config.bucket_name
config.stream_name
config.sagemaker_endpoint_name
config.confidence_threshold     # float, e.g. 0.75
config.sns_topic_arn
config.region
config.cmk_key_arn
config.sagemaker_endpoint_arn   # optional: present only if that parameter exists
```

* `load_config()` never returns a **partial** configuration. If any
  parameter in `DEFAULT_REQUIRED_KEYS` is missing under the resolved
  prefix, it raises `common.config.ConfigError` naming exactly which
  key(s) are missing and under which path prefix — callers should treat
  this as a fatal, retryable-after-fix configuration error, not catch it
  and fall back to a default.
* `load_raw_config()` / `load_config(required_keys=...)` accept a custom
  `required_keys` iterable for callers that only need a subset (e.g. the
  stream-configurator Lambda does not need `sns-topic-arn`).
* Both functions accept an optional `ssm_client` parameter so tests can
  inject a `moto`-backed or stub client instead of hitting real AWS.

### Cold start / latency note

Per the Lambda cold-start budget for this pipeline, `common/config.py`
only imports `boto3` (already present in the Lambda runtime) and
performs no heavy imports. Callers running inside a Lambda handler
should call `load_config()` once per cold start (module-level / outside
the handler function) if the parameter values are not expected to need
to change mid-warm-container-lifetime, or on every invocation if
immediate pickup of parameter changes matters more than the extra SSM
round-trip. This module does not impose either choice — see
`functions/*/app.py` for the policy each Lambda actually uses.

## Destination URI for KVS image generation (`DestinationConfig.Uri`)

The KVS `UpdateImageGenerationConfiguration` destination URI (set by
`stream-configurator`, see `kinesisvideo_dg.txt` pages 245–246) takes the
form:

```
s3://<bucket-name>/<prefix>/
```

`<bucket-name>` must be read from the `bucket-name` SSM parameter (never
hard-coded) and must match the bucket this foundation stack created.

**Randomized-prefix guidance:** at the frame-extraction sampling
intervals this pipeline uses (seconds, not milliseconds, per
`sns_stack`/`inference_stack`'s expected throughput), S3 request-rate
partitioning limits are unlikely to be the bottleneck, but as a matter
of future-proofing and to avoid any single lexical prefix becoming a hot
partition if sampling intervals are later tightened or multiple drones
write concurrently, the destination prefix SHOULD include a short
randomized (non-sequential, non-purely-time-ordered) component ahead of
the human-readable stream name, e.g.:

```
s3://<bucket-name>/<4-hex-char-random>/<stream-name>/
```

e.g. `s3://fire-detection-dev-images-ab12cd34/f3a1/drone-fire-stream/`.
This is a convention this pipeline's `kvs_stack` / stream-configurator is
expected to apply when constructing the `Uri` value — the S3 bucket
itself (this stack's resource) places no restriction on object key
shape and requires no configuration change to support it. Per-fragment
S3 key *suffixes* (the `AWS_KINESISVIDEO_IMAGE_PREFIX` fragment tag) are
set by the producer/drone side and are out of this stack's scope — see
`producer_reference/README.md`.
