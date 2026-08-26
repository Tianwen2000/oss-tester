from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cdn_fixtures import FIXTURE_SPECS, generate_fixture_directory, seed_cdn_fixtures


class FixtureFakeS3:
    def __init__(self, fail_suffix: str | None = None, error_text: str = "upload failed", reject_encoding: bool = False):
        self.fail_suffix = fail_suffix
        self.error_text = error_text
        self.reject_encoding = reject_encoding
        self.uploads: list[dict] = []

    def put_object(self, **kwargs):
        if self.fail_suffix and kwargs["Key"].endswith(self.fail_suffix):
            raise RuntimeError(self.error_text)
        if self.reject_encoding and kwargs["Key"].endswith("gzip.txt") and kwargs.get("ContentEncoding"):
            from oss_test import ClientError
            raise ClientError({"Error": {"Code": "InternalError", "Message": "unsupported Content-Encoding"}}, "PutObject")
        body = kwargs["Body"]
        self.uploads.append({"key": kwargs["Key"], "body_type": type(body), "body": body.read(), "kwargs": kwargs})
        return {"ETag": '"fixture-etag"'}


class CdnFixtureTests(unittest.TestCase):
    def test_generation_creates_all_resources_without_large_memory_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = generate_fixture_directory(directory)
            self.assertEqual(len(FIXTURE_SPECS), 15)
            self.assertTrue((root / "large.bin").stat().st_size >= 8 * 1024 * 1024)
            self.assertEqual(
                gzip.decompress((root / "gzip.txt").read_bytes()),
                b"oss-tester CDN gzip fixture. The object is stored with Content-Encoding: gzip.\n",
            )
            self.assertTrue((root / "redirect" / "301.html").exists())
            self.assertTrue((root / "errors" / "503.html").exists())

    def test_confirmation_and_unique_prefix_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "confirm-bucket"):
                seed_cdn_fixtures(
                    FixtureFakeS3(), bucket="test-bucket", endpoint="http://s3.test", region="test-1",
                    directory=directory, confirm_bucket=False, progress=None,
                )
            first = seed_cdn_fixtures(
                FixtureFakeS3(), bucket="test-bucket", endpoint="http://s3.test", region="test-1",
                directory=directory, base_prefix="cdn-test", confirm_bucket=True, run_id="run-a", progress=None,
            )
            second = seed_cdn_fixtures(
                FixtureFakeS3(), bucket="test-bucket", endpoint="http://s3.test", region="test-1",
                directory=directory, base_prefix="cdn-test", confirm_bucket=True, run_id="run-b", progress=None,
            )
            self.assertNotEqual(first["prefix"], second["prefix"])
            self.assertTrue(first["prefix"].startswith("cdn-test/run-a/"))
            self.assertTrue(all(item["key"].startswith(first["prefix"]) for item in first["objects"]))

    def test_uploads_stream_files_and_writes_safe_manifest(self):
        fake = FixtureFakeS3()
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "reports" / "cdn-{run_id}.json"
            manifest = seed_cdn_fixtures(
                fake, bucket="test-bucket", endpoint="http://s3.test", region="test-1",
                directory=Path(directory) / "fixtures", base_prefix="cdn-test", manifest_path=manifest_path,
                confirm_bucket=True, run_id="run-123", progress=None,
            )
            self.assertEqual(manifest["status"], "PASS")
            self.assertEqual(manifest["object_count"], 15)
            self.assertEqual(len(fake.uploads), 15)
            self.assertTrue(all(item["body_type"] is not bytes for item in fake.uploads))
            gzip_upload = next(item for item in fake.uploads if item["key"].endswith("gzip.txt"))
            self.assertEqual(gzip_upload["kwargs"]["ContentEncoding"], "gzip")
            self.assertEqual(gzip_upload["kwargs"]["CacheControl"], "max-age=300")
            self.assertTrue(Path(manifest["manifest_path"]).exists())
            payload = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["prefix"], "cdn-test/run-123/")
            self.assertNotIn("secret", json.dumps(payload).lower())

    def test_gzip_header_incompatibility_falls_back_to_warning(self):
        fake = FixtureFakeS3(reject_encoding=True)
        with tempfile.TemporaryDirectory() as directory:
            manifest = seed_cdn_fixtures(
                fake, bucket="test-bucket", endpoint="http://s3.test", region="test-1",
                directory=directory, manifest_path=Path(directory) / "manifest.json", confirm_bucket=True,
                run_id="gzip-fallback", progress=None,
            )
            self.assertEqual(manifest["status"], "WARN")
            self.assertEqual(manifest["object_count"], 15)
            self.assertEqual(len(manifest["warnings"]), 1)
            gzip_item = next(item for item in manifest["objects"] if item["path"] == "gzip.txt")
            self.assertIsNone(gzip_item["content_encoding"])
            self.assertEqual(gzip_item["requested_content_encoding"], "gzip")

    def test_partial_upload_failure_returns_fail_and_redacts_credentials(self):
        sentinel = "fixture-secret-value"
        fake = FixtureFakeS3(fail_suffix="errors/503.html", error_text=sentinel)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OSS_SECRET_ACCESS_KEY": sentinel}, clear=False):
            manifest = seed_cdn_fixtures(
                fake, bucket="test-bucket", endpoint="http://s3.test", region="test-1",
                directory=directory, manifest_path=Path(directory) / "manifest.json", confirm_bucket=True,
                run_id="failed-run", progress=None,
            )
            self.assertEqual(manifest["status"], "FAIL")
            self.assertEqual(manifest["object_count"], 14)
            self.assertNotIn(sentinel, Path(manifest["manifest_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
