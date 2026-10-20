"""Content offloaded by the collector to S3-compatible storage (exporter `blobs`), read back for evaluation.

Configured like the collector: EE_S3_ENDPOINT (empty: AWS), EE_S3_REGION, and the standard
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.
"""

import json
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _client():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ.get("EE_S3_ENDPOINT") or None,
                        region_name=os.environ.get("EE_S3_REGION", "us-east-1"))


@lru_cache(maxsize=4096)
def _get(uri: str) -> str:
    bucket, key = uri[len("s3://"):].split("/", 1)
    return _client().get_object(Bucket=bucket, Key=key)["Body"].read().decode()


def read(uri: str) -> dict:
    if not uri.startswith("s3://"):
        raise ValueError(f"unsupported blob uri {uri}")
    return json.loads(_get(uri))
