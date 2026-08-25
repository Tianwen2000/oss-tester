from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import oss_test
from oss_test import (
    CaseWarning,
    DEFAULT_CONFIG,
    OSSRunner,
    apply_cli_overrides,
    build_parser,
    choose_suites,
    load_config,
    main,
    parse_config_override,
    validate_config,
    write_report,
)
from sigv4 import SigV4Signer


def s3_error(code: str, message: str = "error") -> Exception:
    return oss_test.ClientError({"Error": {"Code": code, "Message": message}}, "Fake")


class Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeS3:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.versions: list[dict] = []
        self.uploads: dict[str, dict] = {}
        self.next_upload = 1
        self.versioning = "Enabled"
        self.tags: dict[str, list[dict]] = {}
        self.acl_calls = []

    @staticmethod
    def _body(value):
        if hasattr(value, "read"):
            return value.read()
        return bytes(value)

    def list_buckets(self):
        return {"Buckets": [{"Name": "unit-test-bucket"}]}

    def head_bucket(self, Bucket):
        if Bucket != "unit-test-bucket": raise s3_error("NoSuchBucket")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def put_object(self, Bucket, Key, Body, **kwargs):
        data = self._body(Body)
        version_id = f"v{len(self.versions) + 1}"
        self.objects[Key] = {"body": data, "ContentType": kwargs.get("ContentType"), "Metadata": kwargs.get("Metadata", {}), "ETag": hashlib.md5(data).hexdigest(), "deleted": False}
        self.versions.append({"Key": Key, "VersionId": version_id, "Size": len(data), "IsLatest": True, "body": data})
        return {"ETag": f'"{hashlib.md5(data).hexdigest()}"', "VersionId": version_id}

    def head_object(self, Bucket, Key):
        if Key not in self.objects or self.objects[Key].get("deleted"): raise s3_error("NotFound")
        item = self.objects[Key]
        return {"ContentLength": len(item["body"]), "ContentType": item["ContentType"], "Metadata": item["Metadata"], "ETag": f'"{item["ETag"]}"'}

    def get_object(self, Bucket, Key, Range=None):
        if Key not in self.objects or self.objects[Key].get("deleted"): raise s3_error("NoSuchKey")
        data = self.objects[Key]["body"]
        if Range:
            start, end = Range.removeprefix("bytes=").split("-")
            data = data[int(start): int(end) + 1]
        return {"Body": Body(data), "ContentLength": len(data), "ETag": f'"{self.objects[Key]["ETag"]}"'}

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix) and not self.objects[key].get("deleted"))
        start = int(ContinuationToken or 0)
        page = keys[start:start + MaxKeys]
        result = {"Contents": [{"Key": key, "Size": len(self.objects[key]["body"])} for key in page], "IsTruncated": start + MaxKeys < len(keys)}
        if result["IsTruncated"]: result["NextContinuationToken"] = str(start + MaxKeys)
        return result

    def list_objects(self, Bucket, Prefix="", Delimiter=None, MaxKeys=1000, **kwargs):
        keys = sorted(key for key in self.objects if key.startswith(Prefix) and not self.objects[key].get("deleted"))
        if not Delimiter: return {"Contents": [{"Key": key, "Size": len(self.objects[key]["body"])} for key in keys]}
        common = sorted({key[:key.find(Delimiter, len(Prefix)) + 1] for key in keys if Delimiter in key[len(Prefix):]})
        return {"Contents": [], "CommonPrefixes": [{"Prefix": item} for item in common]}

    def copy_object(self, Bucket, Key, CopySource):
        source = self.objects[CopySource["Key"]]["body"]
        return {"ETag": f'"{hashlib.md5(source).hexdigest()}"', "CopyObjectResult": {"ETag": f'"{hashlib.md5(source).hexdigest()}"'}, **self.put_object(Bucket, Key, source)}

    def put_object_tagging(self, Bucket, Key, Tagging): self.tags[Key] = Tagging["TagSet"]
    def get_object_tagging(self, Bucket, Key): return {"TagSet": self.tags.get(Key, [])}
    def delete_object_tagging(self, Bucket, Key): self.tags.pop(Key, None)
    def get_object_acl(self, Bucket, Key): return {"Owner": {"ID": "owner"}, "Grants": [{"Permission": "FULL_CONTROL"}]}
    def put_object_acl(self, Bucket, Key, ACL): self.acl_calls.append(ACL)

    def get_bucket_versioning(self, Bucket): return {"Status": self.versioning}

    def list_object_versions(self, Bucket, Prefix="", MaxKeys=1000, **kwargs):
        versions = [item.copy() for item in self.versions if item["Key"].startswith(Prefix)]
        markers = [{"Key": key, "VersionId": f"d{index}", "IsLatest": True} for index, key in enumerate(self.objects) if key.startswith(Prefix) and self.objects[key].get("deleted")]
        return {"Versions": versions, "DeleteMarkers": markers, "IsTruncated": False}

    def delete_object(self, Bucket, Key, **kwargs):
        if kwargs.get("VersionId"):
            self.versions = [item for item in self.versions if not (item["Key"] == Key and item["VersionId"] == kwargs["VersionId"])]
        elif self.versioning == "Enabled":
            if Key in self.objects: self.objects[Key]["deleted"] = True
        else: self.objects.pop(Key, None)
        return {"DeleteMarker": self.versioning == "Enabled"}

    def delete_objects(self, Bucket, Delete):
        deleted = []
        for item in Delete["Objects"]:
            key = item["Key"]
            if item.get("VersionId"):
                if str(item["VersionId"]).startswith("d") and key in self.objects:
                    self.objects[key]["deleted"] = False
                self.versions = [v for v in self.versions if not (v["Key"] == key and v["VersionId"] == item["VersionId"])]
            else: self.objects.pop(key, None)
            deleted.append({"Key": key, **({"VersionId": item["VersionId"]} if item.get("VersionId") else {})})
        return {"Deleted": deleted, "Errors": []}

    def create_multipart_upload(self, Bucket, Key, **kwargs):
        upload_id = f"u{self.next_upload}"; self.next_upload += 1
        self.uploads[upload_id] = {"Key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        data = self._body(Body); etag = hashlib.md5(data).hexdigest()
        self.uploads[UploadId]["parts"][PartNumber] = data
        return {"ETag": f'"{etag}"'}

    def list_parts(self, Bucket, Key, UploadId):
        return {"Parts": [{"PartNumber": n, "ETag": f'"{hashlib.md5(data).hexdigest()}"', "Size": len(data)} for n, data in sorted(self.uploads[UploadId]["parts"].items())]}

    def upload_part_copy(self, Bucket, Key, UploadId, PartNumber, CopySource):
        data = self.objects[CopySource["Key"]]["body"]; self.uploads[UploadId]["parts"][PartNumber] = data
        return {"CopyPartResult": {"ETag": f'"{hashlib.md5(data).hexdigest()}"'}}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        data = b"".join(self.uploads[UploadId]["parts"][item["PartNumber"]] for item in MultipartUpload["Parts"])
        self.uploads.pop(UploadId)
        return self.put_object(Bucket, Key, data)

    def abort_multipart_upload(self, Bucket, Key, UploadId): self.uploads.pop(UploadId, None); return {}
    def list_multipart_uploads(self, Bucket, Prefix="", **kwargs): return {"Uploads": [{"Key": item["Key"], "UploadId": upload_id} for upload_id, item in self.uploads.items() if item["Key"].startswith(Prefix)], "IsTruncated": False}


class FakeControlS3(FakeS3):
    def __init__(self):
        super().__init__()
        self.bucket_acl = {"Owner": {"ID": "owner"}, "Grants": [{"Permission": "FULL_CONTROL"}]}
        self.policy = None
        self.lifecycle = {"Rules": [{"ID": "original", "Status": "Enabled", "Filter": {"Prefix": "original/"}, "Expiration": {"Days": 90}}]}
        self.encryption = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
        self.cors = {"CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["https://original.invalid"]}]}
        self.bucket_tags = {"TagSet": [{"Key": "owner", "Value": "qa"}]}

    def get_bucket_acl(self, Bucket): return copy.deepcopy(self.bucket_acl)
    def put_bucket_acl(self, Bucket, ACL=None, AccessControlPolicy=None):
        if AccessControlPolicy is not None: self.bucket_acl = copy.deepcopy(AccessControlPolicy)
        elif ACL == "private": self.bucket_acl = {"Owner": {"ID": "owner"}, "Grants": [{"Permission": "FULL_CONTROL"}]}
    def get_bucket_policy(self, Bucket):
        if self.policy is None: raise s3_error("NoSuchBucketPolicy")
        return {"Policy": self.policy}
    def put_bucket_policy(self, Bucket, Policy): self.policy = Policy
    def delete_bucket_policy(self, Bucket): self.policy = None
    def put_bucket_versioning(self, Bucket, VersioningConfiguration): self.versioning = VersioningConfiguration["Status"]
    def get_bucket_lifecycle_configuration(self, Bucket): return copy.deepcopy(self.lifecycle)
    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration): self.lifecycle = copy.deepcopy(LifecycleConfiguration)
    def delete_bucket_lifecycle(self, Bucket): self.lifecycle = None
    def get_bucket_encryption(self, Bucket): return {"ServerSideEncryptionConfiguration": copy.deepcopy(self.encryption)}
    def put_bucket_encryption(self, Bucket, ServerSideEncryptionConfiguration): self.encryption = copy.deepcopy(ServerSideEncryptionConfiguration)
    def delete_bucket_encryption(self, Bucket): self.encryption = None
    def get_bucket_cors(self, Bucket): return copy.deepcopy(self.cors)
    def put_bucket_cors(self, Bucket, CORSConfiguration): self.cors = copy.deepcopy(CORSConfiguration)
    def delete_bucket_cors(self, Bucket): self.cors = None
    def get_bucket_tagging(self, Bucket): return copy.deepcopy(self.bucket_tags)
    def put_bucket_tagging(self, Bucket, Tagging): self.bucket_tags = copy.deepcopy(Tagging)
    def delete_bucket_tagging(self, Bucket): self.bucket_tags = None


class RunnerTests(unittest.TestCase):
    def config(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["connection"].update(endpoint="http://127.0.0.1:9", region="test-1", bucket="unit-test-bucket")
        config["execution"].update(prefix="oss-test", cleanup="always")
        validate_config(config, require_target=True)
        return config

    def test_config_and_prefix_are_unique(self):
        config = self.config()
        first, second = OSSRunner(config, FakeS3()), OSSRunner(config, FakeS3())
        self.assertNotEqual(first.prefix, second.prefix)
        self.assertTrue(first.prefix.startswith("oss-test:"))
        self.assertTrue(first.prefix.endswith(":"))
        self.assertEqual(choose_suites(config), ["network", "authentication", "data", "multipart"])
        with self.assertRaises(ValueError): parse_config_override("connection.timeout")
        self.assertEqual(choose_suites(config, "data,multipart"), ["data", "multipart"])

    def test_config_file_cli_override_and_sensitive_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"connection": {"endpoint": "https://config.invalid", "region": "r1", "bucket": "unit-test-bucket"}, "execution": {"cleanup": "never"}}), encoding="utf-8")
            with patch("oss_test.load_dotenv"), patch.dict(os.environ, {}, clear=True):
                config = load_config(str(path))
            args = build_parser().parse_args(["--timeout", "12", "--set", "execution.concurrency=3"])
            apply_cli_overrides(config, args)
            validate_config(config, require_target=True)
            self.assertEqual(config["connection"]["timeout"], 12)
            self.assertEqual(config["execution"]["concurrency"], 3)
        bad = self.config(); bad["connection"]["secret_access_key"] = "must-not-be-accepted"
        with self.assertRaisesRegex(ValueError, "Unknown connection option"):
            validate_config(bad, require_target=True)
        bad = self.config(); bad["connection"]["endpoint"] = "https://user:secret@example.invalid"
        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            validate_config(bad, require_target=True)

    def test_data_suite_content_assertions_and_cleanup_scope(self):
        config = self.config(); fake = FakeS3(); runner = OSSRunner(config, fake)
        runner.run_data()
        self.assertFalse(any(item.status == "FAIL" for item in runner.results), [item.error for item in runner.results])
        outside = "other-application/key"
        fake.put_object(Bucket=runner.bucket, Key=outside, Body=b"keep")
        details = runner.cleanup(remove_objects=True)
        self.assertEqual(details["status"], "PASS")
        self.assertIn(outside, fake.objects)
        self.assertFalse(any(key.startswith(runner.prefix) for key in fake.objects))

    def test_corrupt_download_is_a_failure_even_with_python_optimization(self):
        config = self.config(); fake = FakeS3(); runner = OSSRunner(config, fake)
        runner._put_data()
        original_get = fake.get_object
        def corrupt_get(**kwargs):
            response = original_get(**kwargs)
            response["Body"] = Body(response["Body"].read() + b"corrupt")
            return response
        fake.get_object = corrupt_get
        result = runner.run_case("corrupt", runner._get_data)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("byte count mismatch", result.error)

    def test_multipart_respects_five_mib_and_cleans_uploads(self):
        config = self.config(); config["execution"]["multipart_part_size_mb"] = 5
        fake = FakeS3(); runner = OSSRunner(config, fake)
        runner.run_multipart()
        result = next(item for item in runner.results if item.name == "multipart.CreateMultipartUpload")
        complete = next(item for item in runner.results if item.name == "multipart.CompleteMultipartUpload")
        self.assertEqual(result.status, "PASS", result.error)
        self.assertEqual(result.metrics["part_size_bytes"], 5 * 1024 * 1024)
        self.assertEqual(complete.status, "PASS", complete.error)
        self.assertFalse(fake.uploads)

    def test_retry_and_timeout(self):
        config = self.config(); config["connection"].update(retry_attempts=2, timeout=0.01, retry_backoff_seconds=0)
        runner = OSSRunner(config, FakeS3())
        calls = {"count": 0}
        def transient(**kwargs):
            calls["count"] += 1
            if calls["count"] == 1: raise OSError("temporary")
            return {"ok": True}
        runner.client.head_bucket = transient
        self.assertEqual(runner._call("head_bucket", Bucket=runner.bucket), {"ok": True})
        self.assertEqual(calls["count"], 2)

        config["connection"].update(retry_attempts=1, timeout=0.005)
        runner = OSSRunner(config, FakeS3())
        runner.client.head_bucket = lambda **kwargs: time.sleep(0.03)
        with self.assertRaisesRegex(TimeoutError, "exceeded"):
            runner._call("head_bucket", Bucket=runner.bucket)
        time.sleep(0.04)

    def test_never_retains_completed_objects_but_aborts_current_uploads(self):
        config = self.config(); fake = FakeS3(); runner = OSSRunner(config, fake)
        key = runner.key("retained.txt")
        fake.put_object(Bucket=runner.bucket, Key=key, Body=b"keep")
        upload = fake.create_multipart_upload(Bucket=runner.bucket, Key=runner.key("unfinished.bin"))["UploadId"]
        details = runner.cleanup(remove_objects=False)
        self.assertEqual(details["status"], "SKIP")
        self.assertIn(key, fake.objects)
        self.assertNotIn(upload, fake.uploads)

    def test_version_api_warning_still_cleans_visible_objects(self):
        class NoVersions(FakeS3):
            def list_object_versions(self, **kwargs): raise s3_error("NotImplemented", "versions unsupported")
        config = self.config(); fake = NoVersions(); runner = OSSRunner(config, fake)
        key = runner.key("visible.txt"); fake.put_object(Bucket=runner.bucket, Key=key, Body=b"remove")
        details = runner.cleanup(remove_objects=True)
        self.assertEqual(details["status"], "WARN")
        self.assertNotIn(key, fake.objects)

    def test_cleanup_refuses_entries_outside_current_prefix(self):
        class BrokenPrefixFilter(FakeS3):
            def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, **kwargs):
                return {"Contents": [{"Key": "outside/business.txt", "Size": 4}], "IsTruncated": False}
        config = self.config(); fake = BrokenPrefixFilter(); runner = OSSRunner(config, fake)
        fake.objects["outside/business.txt"] = {"body": b"keep", "deleted": False}
        details = runner.cleanup(remove_objects=True)
        self.assertEqual(details["status"], "FAIL")
        self.assertIn("outside/business.txt", fake.objects)

    def test_status_and_report_redact_credentials(self):
        config = self.config(); fake = FakeS3(); runner = OSSRunner(config, fake)
        runner.run_case("warning", lambda: (_ for _ in ()).throw(CaseWarning("unsupported")))
        runner.run_case("failure", lambda: (_ for _ in ()).throw(AssertionError("bad")))
        runner.run_case("service-unavailable", lambda: (_ for _ in ()).throw(s3_error("ServiceUnavailable", "retry exhausted")))
        runner.run_case("data.put_object", lambda: (_ for _ in ()).throw(s3_error("NotImplemented", "core operation")))
        runner.run_case("data.object_tags", lambda: (_ for _ in ()).throw(s3_error("NotImplemented", "optional operation")))
        with tempfile.TemporaryDirectory() as directory:
            path = write_report(runner, config, ["data"], "2026-01-01T00:00:00+00:00", report=str(Path(directory) / "report.json"), exit_code=1)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["FAIL"], 3)
        self.assertEqual(payload["summary"]["WARN"], 2)
        self.assertEqual(payload["overall_status"], "FAIL")
        self.assertNotIn("secret", json.dumps(payload))
        self.assertEqual(payload["endpoint"], config["connection"]["endpoint"])

    def test_credentials_are_redacted_from_console_and_report(self):
        config = self.config(); runner = OSSRunner(config, FakeS3())
        sentinel = "SENTINEL_SECRET_VALUE"
        output = io.StringIO()
        with patch.dict(os.environ, {"OSS_ACCESS_KEY_ID": sentinel, "OSS_SECRET_ACCESS_KEY": sentinel}, clear=False), redirect_stdout(output):
            runner.run_case("redaction", lambda: (_ for _ in ()).throw(RuntimeError(sentinel)))
            with tempfile.TemporaryDirectory() as directory:
                path = write_report(runner, config, ["data"], "2026-01-01T00:00:00+00:00", report=str(Path(directory) / "redacted.json"), exit_code=1)
                report_text = path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, output.getvalue())
        self.assertNotIn(sentinel, report_text)

    def test_control_plane_restores_original_configuration(self):
        config = self.config(); config["safety"]["confirm_control_plane"] = True
        fake = FakeControlS3()
        original = {"acl": copy.deepcopy(fake.bucket_acl), "lifecycle": copy.deepcopy(fake.lifecycle), "encryption": copy.deepcopy(fake.encryption), "cors": copy.deepcopy(fake.cors), "tags": copy.deepcopy(fake.bucket_tags), "versioning": fake.versioning}
        runner = OSSRunner(config, fake)
        runner.run_control_plane()
        self.assertEqual(fake.bucket_acl, original["acl"])
        self.assertEqual(fake.lifecycle, original["lifecycle"])
        self.assertEqual(fake.encryption, original["encryption"])
        self.assertEqual(fake.cors, original["cors"])
        self.assertEqual(fake.bucket_tags, original["tags"])
        self.assertEqual(fake.versioning, original["versioning"])
        self.assertEqual(runner.results[-1].name, "control-plane.restore")
        self.assertEqual(runner.results[-1].status, "PASS")

    def test_main_returns_nonzero_and_writes_report_for_failure(self):
        class Broken(FakeS3):
            def head_bucket(self, Bucket): raise s3_error("AccessDenied", "denied")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failure.json"
            code = main(["--endpoint", "http://127.0.0.1:9", "--region", "test-1", "--bucket", "unit-test-bucket", "--suites", "data", "--report", str(path)], client=Broken())
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["summary"]["FAIL"] >= 1, True)

    def test_interrupt_writes_fail_result_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.json"
            with patch.object(OSSRunner, "run_suite", side_effect=KeyboardInterrupt):
                code = main(["--endpoint", "http://127.0.0.1:9", "--region", "test-1", "--bucket", "unit-test-bucket", "--suites", "data", "--report", str(path)], client=FakeS3())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(code, 130)
            self.assertTrue(payload["interrupted"])
            self.assertEqual(payload["summary"]["FAIL"], 1)

    def test_cleanup_failure_causes_nonzero_exit(self):
        class CleanupBroken(FakeS3):
            def list_multipart_uploads(self, **kwargs): raise s3_error("AccessDenied", "cleanup denied")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cleanup-failure.json"
            code = main(["--endpoint", "http://127.0.0.1:9", "--region", "test-1", "--bucket", "unit-test-bucket", "--suites", "security", "--report", str(path)], client=CleanupBroken())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(code, 1)
            self.assertEqual(payload["cleanup"]["status"], "FAIL")
            self.assertGreaterEqual(payload["summary"]["FAIL"], 1)


class SignatureTests(unittest.TestCase):
    def test_sigv4_is_deterministic_and_does_not_expose_secret(self):
        signer = SigV4Signer("AKID", "SECRET", "us-test-1", clock=lambda: "20240101T000000Z")
        signed = signer.sign("GET", "/bucket/key", {"x": "1", "empty": ""}, {"host": "example.test"}, b"")
        self.assertIn("AWS4-HMAC-SHA256", signed["Authorization"])
        self.assertNotIn("SECRET", signed["Authorization"])
        self.assertEqual(signed["x-amz-date"], "20240101T000000Z")


if __name__ == "__main__": unittest.main()
