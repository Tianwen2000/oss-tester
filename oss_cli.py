#!/usr/bin/env python3
"""Compatibility CLI for the original OSS commands.

New acceptance/regression runs belong to ``oss_test.py``.  This file keeps the
old read/write command names usable while routing SDK construction through the
same boto3 configuration and applying explicit safety checks to destructive
operations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from oss_test import _safe_name, create_s3_client, load_config, validate_config
from sigv4 import HttpS3Client, SigV4Signer, parse_error


def get_s3() -> Any:
    """Backward-compatible boto3 client factory using the unified config."""
    return create_s3_client(load_config())


def _client(args: argparse.Namespace) -> tuple[Any, str]:
    config = load_config()
    for attr, section_key in (("endpoint", "endpoint"), ("region", "region"), ("bucket", "bucket")):
        value = getattr(args, attr, None)
        if value: config["connection"][section_key] = value
    validate_config(config, require_target=True)
    return create_s3_client(config), str(config["connection"]["bucket"])


def _confirm(args: argparse.Namespace, action: str) -> None:
    if not getattr(args, "confirm_risk", False):
        raise SystemExit(
            f"Refusing to run {action}: pass --confirm-risk explicitly. "
            "For acceptance tests, prefer the unique run prefix managed by oss_test.py."
        )


def _read_body(response: dict[str, Any], destination: str | None = None) -> tuple[int, str]:
    digest = hashlib.sha256(); total = 0
    stream = response["Body"]
    target = open(destination, "wb") if destination else None
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk: break
            digest.update(chunk); total += len(chunk)
            if target: target.write(chunk)
    finally:
        if target: target.close()
        close = getattr(stream, "close", None)
        if close: close()
    return total, digest.hexdigest()


def run_legacy(args: argparse.Namespace) -> int:
    if args.command in {"check", "buckets"}:
        config = load_config()
        for attr, section_key in (("endpoint", "endpoint"), ("region", "region"), ("bucket", "bucket")):
            value = getattr(args, attr, None)
            if value: config["connection"][section_key] = value
        validate_config(config, require_target=True)
        s3 = create_s3_client(config)
        if args.command == "check": s3.list_buckets(); print("Connectivity and credential authentication succeeded"); return 0
        response = s3.list_buckets(); print(f"Buckets: {len(response.get('Buckets', []))}")
        for item in response.get("Buckets", []): print(f"  - {item.get('Name')} (created: {item.get('CreationDate')})")
        return 0

    s3, bucket = _client(args)
    key = getattr(args, "key", None)
    if args.command == "demo":
        s3.head_bucket(Bucket=bucket); listing = s3.list_objects_v2(Bucket=bucket, MaxKeys=10)
        print(f"HeadBucket OK; ListObjectsV2={len(listing.get('Contents', []))}"); return 0
    if args.command in {"list", "list-v1"}:
        kw = {"Bucket": bucket, "MaxKeys": args.max_keys}
        if args.prefix: kw["Prefix"] = args.prefix
        if args.delimiter: kw["Delimiter"] = args.delimiter
        response = s3.list_objects(Bucket=bucket, **kw) if args.command == "list-v1" else s3.list_objects_v2(Bucket=bucket, **kw)
        for item in response.get("Contents", []): print(f"{item['Key']} size={item.get('Size')}")
        for item in response.get("CommonPrefixes", []): print(item.get("Prefix"))
        print(f"Objects: {len(response.get('Contents', []))}"); return 0
    if args.command == "versions":
        response = s3.list_object_versions(Bucket=bucket, Prefix=args.prefix or "", MaxKeys=args.max_keys)
        print(f"versions={len(response.get('Versions', []))}, delete_markers={len(response.get('DeleteMarkers', []))}"); return 0
    if args.command == "mp-list":
        response = s3.list_multipart_uploads(Bucket=bucket, Prefix=args.prefix or "")
        print(f"uploads={len(response.get('Uploads', []))}"); return 0
    if args.command == "head":
        response = s3.head_object(Bucket=bucket, Key=key); print(f"{key}: size={response.get('ContentLength')} etag={response.get('ETag')}"); return 0
    if args.command == "bucket-head":
        s3.head_bucket(Bucket=bucket); print(f"{bucket} exists"); return 0
    if args.command == "bucket-location":
        print(s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")); return 0
    if args.command == "bucket-create":
        _confirm(args, "CreateBucket"); s3.create_bucket(Bucket=bucket); print(f"Bucket created: {bucket}"); return 0
    if args.command == "bucket-version-get":
        print(s3.get_bucket_versioning(Bucket=bucket)); return 0
    if args.command == "bucket-version":
        _confirm(args, "PutBucketVersioning"); s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}); print("Versioning=Enabled"); return 0
    if args.command == "bucket-acl":
        s3.put_bucket_acl(Bucket=bucket, ACL="private"); print("Bucket ACL=private"); return 0
    if args.command in {"bucket-lifecycle", "bucket-encryption", "bucket-cors", "bucket-tag", "bucket-policy"}:
        _confirm(args, args.command)
        if args.command == "bucket-lifecycle":
            s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{"ID": "oss-tester", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 365}}]})
        elif args.command == "bucket-encryption":
            s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        elif args.command == "bucket-cors":
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [{"AllowedHeaders": ["*"], "AllowedMethods": ["GET", "HEAD"], "AllowedOrigins": ["https://example.invalid"]}]})
        elif args.command == "bucket-tag":
            tags = [{"Key": part.split("=", 1)[0], "Value": part.split("=", 1)[1]} for part in args.tags.split(",") if "=" in part]
            s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": tags})
        else:
            policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/*"}]}
            s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
        print(f"{args.command} OK"); return 0
    if args.command == "upload":
        with open(args.file, "rb") as stream: response = s3.put_object(Bucket=bucket, Key=args.key or Path(args.file).name, Body=stream)
        print(f"Upload succeeded: {args.key or Path(args.file).name} etag={response.get('ETag')}"); return 0
    if args.command == "mp-upload":
        part_size = args.part_size * 1024 * 1024
        if part_size < 5 * 1024 * 1024: raise SystemExit("Multipart part size must be at least 5 MiB, except for the final part")
        object_key = args.key or Path(args.file).name
        upload_id = s3.create_multipart_upload(Bucket=bucket, Key=object_key)["UploadId"]
        parts = []
        try:
            with open(args.file, "rb") as stream:
                number = 1
                while True:
                    chunk = stream.read(part_size)
                    if not chunk: break
                    response = s3.upload_part(Bucket=bucket, Key=object_key, UploadId=upload_id, PartNumber=number, Body=chunk)
                    parts.append({"PartNumber": number, "ETag": response["ETag"]}); number += 1
            s3.complete_multipart_upload(Bucket=bucket, Key=object_key, UploadId=upload_id, MultipartUpload={"Parts": parts})
        except Exception:
            try: s3.abort_multipart_upload(Bucket=bucket, Key=object_key, UploadId=upload_id)
            except Exception: pass
            raise
        print(f"Multipart upload succeeded: {object_key}, parts={len(parts)}"); return 0
    if args.command == "download":
        response = s3.get_object(Bucket=bucket, Key=key); size, digest = _read_body(response, args.dest or Path(key).name)
        print(f"Download succeeded: {key} bytes={size} sha256={digest}"); return 0
    if args.command == "copy":
        response = s3.copy_object(Bucket=bucket, Key=args.dest, CopySource={"Bucket": bucket, "Key": args.src})
        print(f"Copy succeeded: {args.src} -> {args.dest} etag={response.get('CopyObjectResult', {}).get('ETag') or response.get('ETag')}"); return 0
    if args.command == "delete":
        _confirm(args, "DeleteObject"); s3.delete_object(Bucket=bucket, Key=key); print(f"Deleted: {key}"); return 0
    if args.command == "delete-multi":
        _confirm(args, "DeleteObjects"); keys = [item.strip() for item in args.keys.split(",") if item.strip()]
        response = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": item} for item in keys], "Quiet": False})
        if response.get("Errors"): raise SystemExit(f"Bulk delete failed: {response['Errors']}")
        print(f"Bulk delete succeeded: {len(response.get('Deleted', []))}"); return 0
    if args.command == "get-acl": print(s3.get_object_acl(Bucket=bucket, Key=key)); return 0
    if args.command == "put-acl":
        if args.acl == "public-read": _confirm(args, "public-read ACL")
        s3.put_object_acl(Bucket=bucket, Key=key, ACL=args.acl); print(f"ACL={args.acl}"); return 0
    if args.command in {"put-tag", "get-tag", "del-tag"}:
        if args.command == "put-tag":
            tags = [{"Key": part.split("=", 1)[0], "Value": part.split("=", 1)[1]} for part in args.tags.split(",") if "=" in part]
            s3.put_object_tagging(Bucket=bucket, Key=key, Tagging={"TagSet": tags})
        elif args.command == "get-tag": print(s3.get_object_tagging(Bucket=bucket, Key=key))
        else: s3.delete_object_tagging(Bucket=bucket, Key=key)
        return 0
    if args.command == "mp-parts":
        response = s3.list_parts(Bucket=bucket, Key=key, UploadId=args.upload_id); print(f"parts={len(response.get('Parts', []))}"); return 0
    if args.command == "mp-abort":
        _confirm(args, "AbortMultipartUpload"); s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=args.upload_id); print("Multipart upload aborted"); return 0
    if args.command == "mp-copy":
        response = s3.upload_part_copy(Bucket=bucket, Key=key, UploadId=args.upload_id, PartNumber=args.part_number, CopySource={"Bucket": bucket, "Key": args.src}); print(response); return 0
    if args.command == "bucket-delete":
        _confirm(args, "DeleteBucket")
        if not args.danger_confirm: raise SystemExit("Bucket deletion also requires --danger-confirm; the test runner never deletes buckets by default")
        s3.delete_bucket(Bucket=bucket); print(f"Bucket deleted: {bucket}"); return 0
    raise SystemExit(f"Unsupported compatibility command: {args.command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Legacy OSS CLI; use oss_test.py for repeatable tests")
    parser.add_argument("--endpoint"); parser.add_argument("--region"); parser.add_argument("--bucket"); parser.add_argument("--confirm-risk", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "buckets", "demo", "bucket-head", "bucket-location", "bucket-create", "bucket-version", "bucket-version-get", "bucket-acl", "bucket-lifecycle", "bucket-encryption", "bucket-cors", "bucket-policy"): sub.add_parser(name)
    for name in ("list", "list-v1", "versions", "mp-list"):
        p = sub.add_parser(name); p.add_argument("--prefix"); p.add_argument("--delimiter"); p.add_argument("--max-keys", type=int, default=1000)
    p = sub.add_parser("head"); p.add_argument("--key", required=True)
    p = sub.add_parser("upload"); p.add_argument("--file", required=True); p.add_argument("--key")
    p = sub.add_parser("mp-upload"); p.add_argument("--file", required=True); p.add_argument("--key"); p.add_argument("--part-size", type=int, default=8)
    p = sub.add_parser("download"); p.add_argument("--key", required=True); p.add_argument("--dest")
    p = sub.add_parser("copy"); p.add_argument("--src", required=True); p.add_argument("--dest", required=True)
    p = sub.add_parser("delete"); p.add_argument("--key", required=True)
    p = sub.add_parser("delete-multi"); p.add_argument("--keys", required=True)
    p = sub.add_parser("get-acl"); p.add_argument("--key", required=True)
    p = sub.add_parser("put-acl"); p.add_argument("--key", required=True); p.add_argument("--acl", choices=["private", "public-read"], default="private")
    for name in ("put-tag", "get-tag", "del-tag"):
        p = sub.add_parser(name); p.add_argument("--key", required=True)
        if name == "put-tag": p.add_argument("--tags", required=True)
    p = sub.add_parser("mp-parts"); p.add_argument("--key", required=True); p.add_argument("--upload-id", required=True)
    p = sub.add_parser("mp-abort"); p.add_argument("--key", required=True); p.add_argument("--upload-id", required=True)
    p = sub.add_parser("mp-copy"); p.add_argument("--key", required=True); p.add_argument("--src", required=True); p.add_argument("--upload-id", required=True); p.add_argument("--part-number", type=int, required=True)
    p = sub.add_parser("bucket-delete"); p.add_argument("--danger-confirm", action="store_true"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("bucket-tag"); p.add_argument("--tags", required=True)
    sub.choices["bucket-acl"].add_argument("--private", action="store_true")
    for child in sub.choices.values():
        child.add_argument("--endpoint", default=argparse.SUPPRESS)
        child.add_argument("--region", default=argparse.SUPPRESS)
        child.add_argument("--bucket", default=argparse.SUPPRESS)
        child.add_argument("--confirm-risk", action="store_true", default=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try: return run_legacy(args)
    except Exception as exc:
        print(f"Error: {_safe_name(f'{type(exc).__name__}: {exc}')}", file=os.sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
