#!/usr/bin/env python3
"""Repeatable, safe S3-compatible OSS data-plane acceptance tests.

The runner intentionally uses boto3 for all SDK operations.  A client can be
injected in unit tests, so the default test suite never needs a real service.
Credentials are resolved by boto3's normal provider chain or the legacy
``OSS_ACCESS_KEY_ID``/``OSS_SECRET_ACCESS_KEY`` environment variables and are
never included in console output or reports.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import re
import signal
import socket
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # --help and offline unit tests do not require python-dotenv.
    load_dotenv = lambda: None  # type: ignore[assignment]

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError, EndpointConnectionError
except ImportError:  # Keep configuration/help usable before dependencies are installed.
    boto3 = None  # type: ignore[assignment]
    Config = None  # type: ignore[assignment,misc]

    class ClientError(Exception):  # type: ignore[no-redef]
        def __init__(self, response: dict[str, Any], operation_name: str):
            self.response = response
            self.operation_name = operation_name
            super().__init__(f"{response.get('Error', {}).get('Code', 'Unknown')}: {response.get('Error', {}).get('Message', '')}")

    class EndpointConnectionError(OSError):  # type: ignore[no-redef]
        pass


PROJECT_ROOT = Path(__file__).resolve().parent
REPORT_SCHEMA_VERSION = 1
STATUSES = ("PASS", "FAIL", "WARN", "SKIP")
NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
UNSUPPORTED_CODES = {
    "NotImplemented", "NotSupported", "XNotImplemented", "InvalidRequest",
    "MethodNotAllowed", "NotImplementedException", "501",
}
RETRYABLE_CODES = {
    "RequestTimeout", "RequestTimeTooSkewed", "SlowDown", "InternalError",
    "ServiceUnavailable", "503", "500",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "connection": {
        "endpoint": None,
        "region": None,
        "bucket": None,
        "credential_profile": None,
        "timeout": 30.0,
        "retry_attempts": 3,
        "retry_backoff_seconds": 0.25,
        "verify_tls": True,
    },
    "execution": {
        "profile": "standard",
        "suites": None,
        "prefix": "oss-test",
        "cleanup": "always",
        "concurrency": 4,
        "multipart_part_size_mb": 5,
        "multipart_pause_seconds": 0.1,
        "performance_objects": 8,
        "performance_object_size_kb": 64,
    },
    "safety": {
        "confirm_bucket": False,
        "confirm_control_plane": False,
        "confirm_risk": False,
        "allow_public_acl": False,
        "allow_public_policy": False,
        "object_acl": None,
    },
    "report": {"directory": "reports"},
}

PROFILES: dict[str, list[str]] = {
    "smoke": ["network", "authentication", "smoke"],
    "standard": ["network", "authentication", "data", "multipart"],
    "performance": ["network", "authentication", "data", "multipart", "performance"],
    "multipart": ["network", "authentication", "multipart"],
    "security": ["network", "authentication", "security"],
    "control-plane": ["network", "authentication", "control-plane"],
}
AVAILABLE_SUITES = {
    "network", "authentication", "smoke", "data", "multipart", "performance",
    "security", "control-plane",
}
OPTIONAL_CASES = {
    "data.list_objects_v1_prefix_delimiter",
    "data.object_tags",
    "data.object_acl",
    "data.versioning",
    "multipart.UploadPartCopy",
}


class CaseWarning(Exception):
    """The service lacks a non-core feature or a result needs review."""


class CaseSkip(Exception):
    """A suite is not applicable to the selected service/configuration."""


class RunInterrupted(BaseException):
    """Signal carrying interruption information through cleanup/reporting."""

    def __init__(self, signum: int):
        super().__init__(f"signal {signum}")
        self.signum = signum


@dataclass
class TestResult:
    name: str
    status: str
    duration_ms: float
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_config_override(expression: str) -> tuple[str, str, Any]:
    if "=" not in expression or "." not in expression.split("=", 1)[0]:
        raise ValueError("--set must use SECTION.OPTION=VALUE")
    path, raw = expression.split("=", 1)
    section, option = (part.strip() for part in path.split(".", 1))
    if section not in DEFAULT_CONFIG or option not in DEFAULT_CONFIG[section]:
        raise ValueError(f"Unknown config option: {section}.{option}")
    return section, option, _parse_value(raw)


def load_config(path: str | None = None) -> dict[str, Any]:
    load_dotenv()
    config = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        source = Path(path).expanduser().resolve()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"Config file not found: {source}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read config file {source}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Config root must be a JSON object")
        config = deep_merge(config, payload)

    env_map = {
        "OSS_ENDPOINT": ("connection", "endpoint"),
        "OSS_REGION": ("connection", "region"),
        "OSS_BUCKET": ("connection", "bucket"),
        "OSS_PREFIX": ("execution", "prefix"),
        "OSS_NAMESPACE": ("execution", "prefix"),
        "AWS_PROFILE": ("connection", "credential_profile"),
    }
    for env_name, (section, option) in env_map.items():
        value = os.getenv(env_name)
        if value:
            config[section][option] = value
    return config


def apply_cli_overrides(config: dict[str, Any], args: argparse.Namespace) -> None:
    for expression in getattr(args, "config_overrides", []) or []:
        section, option, value = parse_config_override(expression)
        config[section][option] = value
    mapping = {
        "endpoint": ("connection", "endpoint"),
        "region": ("connection", "region"),
        "bucket": ("connection", "bucket"),
        "credential_profile": ("connection", "credential_profile"),
        "timeout": ("connection", "timeout"),
        "retry_attempts": ("connection", "retry_attempts"),
        "retry_backoff": ("connection", "retry_backoff_seconds"),
        "concurrency": ("execution", "concurrency"),
    }
    for arg_name, (section, option) in mapping.items():
        value = getattr(args, arg_name, None)
        if value is not None:
            config[section][option] = value
    profile = getattr(args, "profile", None)
    if profile:
        config["execution"]["profile"] = profile
    suites = getattr(args, "suites", None)
    if suites:
        config["execution"]["suites"] = [s.strip() for s in suites.split(",") if s.strip()]
    prefix = getattr(args, "prefix", None) or getattr(args, "namespace", None)
    if prefix:
        config["execution"]["prefix"] = prefix.rstrip(":")
    cleanup = getattr(args, "cleanup", None)
    if cleanup:
        config["execution"]["cleanup"] = cleanup
    for attr, option in (("confirm_bucket", "confirm_bucket"),
                         ("confirm_control_plane", "confirm_control_plane"),
                         ("confirm_risk", "confirm_risk"),
                         ("allow_public_acl", "allow_public_acl"),
                         ("allow_public_policy", "allow_public_policy")):
        if getattr(args, attr, False):
            config["safety"][option] = True


def validate_config(config: dict[str, Any], *, require_target: bool = False) -> None:
    if not isinstance(config, dict):
        raise ValueError("Config root must be a JSON object")
    unknown_sections = sorted(set(config) - set(DEFAULT_CONFIG))
    if unknown_sections:
        raise ValueError(f"Unknown config section(s): {', '.join(unknown_sections)}")
    for section, defaults in DEFAULT_CONFIG.items():
        value = config.get(section)
        if not isinstance(value, dict):
            raise ValueError(f"{section} must be a JSON object")
        unknown = sorted(set(value) - set(defaults))
        if unknown:
            raise ValueError(f"Unknown {section} option(s): {', '.join(unknown)}")

    connection = config["connection"]
    endpoint = connection.get("endpoint")
    if require_target and (not isinstance(endpoint, str) or not endpoint.strip()):
        raise ValueError("--endpoint or OSS_ENDPOINT is required")
    if endpoint is not None:
        parsed = urlparse(str(endpoint))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("connection.endpoint must be an http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("connection.endpoint must not contain credentials")
    region = connection.get("region")
    if region is not None and (not isinstance(region, str) or not region.strip()):
        raise ValueError("connection.region must be a non-empty string")
    bucket = connection.get("bucket")
    if require_target and (not isinstance(bucket, str) or not bucket.strip()):
        raise ValueError("--bucket or OSS_BUCKET is required; use a dedicated test bucket")
    if bucket is not None:
        if not isinstance(bucket, str) or not BUCKET_PATTERN.fullmatch(bucket):
            raise ValueError("connection.bucket must be a DNS-compatible S3 bucket name")
    timeout = connection.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < float(timeout) <= 300:
        raise ValueError("connection.timeout must be between 0 and 300 seconds")
    attempts = connection.get("retry_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 5:
        raise ValueError("connection.retry_attempts must be an integer between 1 and 5")
    backoff = connection.get("retry_backoff_seconds")
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or not 0 <= float(backoff) <= 5:
        raise ValueError("connection.retry_backoff_seconds must be between 0 and 5")
    if not isinstance(connection.get("verify_tls"), bool):
        raise ValueError("connection.verify_tls must be true or false")

    execution = config["execution"]
    if execution.get("profile") not in PROFILES:
        raise ValueError(f"Unknown profile: {execution.get('profile')}")
    if execution.get("cleanup") not in {"always", "on-success", "never"}:
        raise ValueError("cleanup must be always, on-success, or never")
    prefix = execution.get("prefix")
    if not isinstance(prefix, str) or not NAMESPACE_PATTERN.fullmatch(prefix.rstrip(":")):
        raise ValueError("execution.prefix must use letters, digits, dots, underscores, colons, or hyphens")
    for key, low, high in (("concurrency", 1, 8), ("multipart_part_size_mb", 5, 512),
                           ("performance_objects", 1, 1000), ("performance_object_size_kb", 1, 10240)):
        value = execution.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"execution.{key} must be between {low} and {high}")
    pause = execution.get("multipart_pause_seconds")
    if isinstance(pause, bool) or not isinstance(pause, (int, float)) or not 0 <= float(pause) <= 60:
        raise ValueError("execution.multipart_pause_seconds must be between 0 and 60")
    suites = execution.get("suites")
    if suites is not None and (not isinstance(suites, list) or not all(isinstance(item, str) for item in suites)):
        raise ValueError("execution.suites must be null or a list of suite names")
    for key, value in config["safety"].items():
        if key == "object_acl":
            if value not in {None, "private", "public-read"}:
                raise ValueError("safety.object_acl must be null, private, or public-read")
        elif not isinstance(value, bool):
            raise ValueError(f"safety.{key} must be true or false")
    report_dir = config["report"].get("directory")
    if not isinstance(report_dir, str) or not report_dir.strip():
        raise ValueError("report.directory must be a non-empty path")


def choose_suites(config: dict[str, Any], override: str | None = None) -> list[str]:
    if override:
        selected = [item.strip() for item in override.split(",") if item.strip()]
    elif config["execution"].get("suites") is not None:
        selected = list(config["execution"]["suites"])
    else:
        selected = list(PROFILES[config["execution"]["profile"]])
    unknown = sorted(set(selected) - AVAILABLE_SUITES)
    if unknown:
        raise ValueError(f"Unknown suite(s): {', '.join(unknown)}")
    if not selected:
        raise ValueError("At least one suite must be selected")
    if len(selected) != len(set(selected)):
        raise ValueError("Duplicate suite names are not allowed")
    return selected


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "Unknown"))
    return type(exc).__name__


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return f"{error.get('Code', 'Unknown')}: {error.get('Message', str(exc))}"
    return f"{type(exc).__name__}: {exc}"


def _not_found(exc: BaseException) -> bool:
    return _error_code(exc) in {
        "404", "NoSuchKey", "NoSuchBucket", "NotFound", "NotFoundException",
        "NoSuchBucketPolicy", "NoSuchLifecycleConfiguration",
        "ServerSideEncryptionConfigurationNotFoundError",
        "NoSuchCORSConfiguration", "NoSuchTagSet",
    }


def _unsupported(exc: BaseException) -> bool:
    return _error_code(exc) in UNSUPPORTED_CODES


def _safe_name(value: str) -> str:
    redacted = value
    for env_name in (
        "OSS_ACCESS_KEY_ID", "OSS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    ):
        secret = os.getenv(env_name)
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted.replace("\n", " ").replace("\r", " ")[:500]


def _read_stream(body: Any, *, sink: Any = None, chunk_size: int = 64 * 1024) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = body.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        if sink is not None:
            sink.write(chunk)
    return total, digest.hexdigest()


def _consume_response(response: dict[str, Any]) -> tuple[int, str]:
    body = response["Body"]
    try:
        return _read_stream(body)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


def _etag(value: Any) -> str:
    return str(value or "").strip('"')


def require(condition: Any, message: str) -> None:
    """Raise a test failure even when Python optimization disables assert."""
    if not condition:
        raise AssertionError(message)


def _api_payload(value: Any) -> Any:
    """Remove boto3 response metadata before replaying a saved configuration."""
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key != "ResponseMetadata"}


class OSSRunner:
    """Execute suites against a boto3-compatible S3 client."""

    def __init__(self, config: dict[str, Any], client: Any = None):
        self.config = config
        self.bucket = str(config["connection"].get("bucket") or "")
        self.endpoint = str(config["connection"].get("endpoint") or "")
        self.region = str(config["connection"].get("region") or "")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{timestamp}-{uuid.uuid4().hex[:10]}"
        namespace = str(config["execution"]["prefix"]).rstrip(":")
        self.prefix = f"{namespace}:{self.run_id}:"
        self.client = client
        self.results: list[TestResult] = []
        self.cleanup_result: dict[str, Any] = {"status": "SKIP", "objects": 0, "versions": 0, "delete_markers": 0, "multipart_uploads": 0, "errors": []}
        self.control_plane_state: dict[str, Any] = {"snapshots": [], "changes": [], "restored": []}
        self.interrupted = False
        self.interruption_reason: str | None = None
        self._expected: dict[str, Any] = {}
        self._last_attempts = 0

    def key(self, suffix: str) -> str:
        return f"{self.prefix}{suffix}"

    def _client(self) -> Any:
        if self.client is None:
            self.client = create_s3_client(self.config)
        return self.client

    def _call(self, method: str, **kwargs: Any) -> Any:
        client = self._client()
        operation = getattr(client, method)
        attempts = int(self.config["connection"]["retry_attempts"])
        backoff = float(self.config["connection"]["retry_backoff_seconds"])
        timeout = float(self.config["connection"]["timeout"])
        last: BaseException | None = None
        body = kwargs.get("Body")
        body_position = body.tell() if hasattr(body, "tell") and hasattr(body, "seek") else None
        for attempt in range(1, attempts + 1):
            self._last_attempts = attempt
            try:
                if attempt > 1 and body_position is not None:
                    body.seek(body_position)
                # boto3's connect/read timeout is authoritative.  This future only
                # bounds mocks or custom clients that otherwise block indefinitely.
                if hasattr(client, "meta"):
                    return operation(**kwargs)
                else:
                    executor = ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(operation, **kwargs)
                    try:
                        return future.result(timeout=timeout)
                    finally:
                        executor.shutdown(wait=False, cancel_futures=True)
            except FutureTimeout as exc:
                last = TimeoutError(f"{method} exceeded {timeout}s")
            except (OSError, TimeoutError, EndpointConnectionError) as exc:
                last = exc
            except ClientError as exc:
                last = exc
                if _error_code(exc) not in RETRYABLE_CODES and not _error_code(exc).startswith("5"):
                    raise
            if attempt < attempts:
                time.sleep(min(backoff * (2 ** (attempt - 1)), 5.0))
        if last is None:
            raise RuntimeError("retry loop ended without a result or exception")
        raise last

    def run_case(self, name: str, function: Callable[[], dict[str, Any] | str | None]) -> TestResult:
        started = time.perf_counter()
        status = "PASS"
        error: str | None = None
        metrics: dict[str, Any] = {}
        try:
            value = function()
            if isinstance(value, dict):
                metrics = value
            elif value:
                metrics = {"detail": str(value)}
        except CaseWarning as exc:
            status, error = "WARN", _safe_name(str(exc))
        except CaseSkip as exc:
            status, error = "SKIP", _safe_name(str(exc))
        except ClientError as exc:
            if _unsupported(exc) and (name in OPTIONAL_CASES or name.startswith("control-plane.")):
                status, error = "WARN", _safe_name(_error_message(exc))
            else:
                status, error = "FAIL", _safe_name(_error_message(exc))
        except AssertionError as exc:
            status, error = "FAIL", _safe_name(str(exc))
        except Exception as exc:
            status, error = "FAIL", _safe_name(_error_message(exc))
        result = TestResult(name, status, round((time.perf_counter() - started) * 1000, 2), error, metrics)
        self.results.append(result)
        detail = error or json.dumps(metrics, ensure_ascii=False, separators=(",", ":")) if (error or metrics) else ""
        print(f"[{status:<4}] {name:<34} {result.duration_ms:>9.2f} ms  {detail}")
        return result

    def run_suite(self, name: str) -> None:
        method = getattr(self, f"run_{name.replace('-', '_')}")
        method()

    def run_network(self) -> None:
        def probe() -> dict[str, Any]:
            parsed = urlparse(self.endpoint)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            attempts = int(self.config["connection"]["retry_attempts"])
            backoff = float(self.config["connection"]["retry_backoff_seconds"])
            for attempt in range(1, attempts + 1):
                self._last_attempts = attempt
                try:
                    with socket.create_connection((parsed.hostname, port), timeout=float(self.config["connection"]["timeout"])):
                        pass
                    return {"host": parsed.hostname, "port": port, "attempts": attempt}
                except OSError:
                    if attempt == attempts:
                        raise
                    time.sleep(min(backoff * (2 ** (attempt - 1)), 5.0))
            raise RuntimeError("network retry loop ended unexpectedly")
        self.run_case("network.connectivity", probe)

    def run_authentication(self) -> None:
        def auth() -> dict[str, Any]:
            self._call("head_bucket", Bucket=self.bucket)
            return {"authenticated": True, "attempts": self._last_attempts}
        self.run_case("authentication.head_bucket", auth)

    def _put_data(self) -> dict[str, Any]:
        data = bytes((index % 251 for index in range(128 * 1024)))
        key = self.key("data/object.txt")
        response = self._call("put_object", Bucket=self.bucket, Key=key, Body=data, ContentType="application/octet-stream", Metadata={"oss-tester": self.run_id})
        self._expected["object"] = data
        return {"key": key, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "etag": _etag(response.get("ETag")), "attempts": self._last_attempts}

    def _head_data(self) -> dict[str, Any]:
        key = self.key("data/object.txt")
        response = self._call("head_object", Bucket=self.bucket, Key=key)
        expected = self._expected["object"]
        require(int(response.get("ContentLength", -1)) == len(expected), "HeadObject ContentLength mismatch")
        require(response.get("ContentType") == "application/octet-stream", "HeadObject ContentType mismatch")
        metadata = {str(k).lower(): str(v) for k, v in (response.get("Metadata") or {}).items()}
        require(metadata.get("oss-tester") == self.run_id, "HeadObject metadata mismatch")
        etag = _etag(response.get("ETag"))
        if not etag:
            raise CaseWarning("HeadObject did not return an ETag")
        expected_etag = hashlib.md5(expected).hexdigest()
        if "-" not in etag and etag != expected_etag:
            raise CaseWarning("HeadObject ETag is not the single-part MD5; content SHA-256 remains authoritative")
        return {"key": key, "content_length": response.get("ContentLength"), "content_type": response.get("ContentType"), "etag": etag, "metadata": metadata}

    def _get_data(self) -> dict[str, Any]:
        key = self.key("data/object.txt")
        response = self._call("get_object", Bucket=self.bucket, Key=key)
        body = response["Body"]
        context = body if hasattr(body, "__enter__") else _BodyContext(body)
        with context as stream:
            size, digest = _read_stream(stream)
        expected = self._expected["object"]
        require(size == len(expected), "GetObject byte count mismatch")
        require(digest == hashlib.sha256(expected).hexdigest(), "GetObject SHA-256 mismatch")
        return {"key": key, "bytes": size, "sha256": digest, "etag": _etag(response.get("ETag"))}

    def _overwrite(self) -> dict[str, Any]:
        key = self.key("data/object.txt")
        data = b"overwrite:" + self.run_id.encode("ascii")
        self._call("put_object", Bucket=self.bucket, Key=key, Body=data, ContentType="text/plain", Metadata={"overwrite": "true"})
        response = self._call("get_object", Bucket=self.bucket, Key=key)
        size, digest = _consume_response(response)
        require(size == len(data) and digest == hashlib.sha256(data).hexdigest(), "overwrite content mismatch")
        self._expected["object"] = data
        return {"key": key, "bytes": size, "sha256": digest}

    def _range(self) -> dict[str, Any]:
        key = self.key("data/object.txt")
        response = self._call("get_object", Bucket=self.bucket, Key=key, Range="bytes=0-15")
        size, digest = _consume_response(response)
        expected = self._expected["object"][:16]
        require(size == len(expected) and digest == hashlib.sha256(expected).hexdigest(), "Range download mismatch")
        return {"key": key, "range": "bytes=0-15", "bytes": size, "sha256": digest}

    def _list_v2(self) -> dict[str, Any]:
        prefix = self.key("list/")
        for index in range(5):
            self._call("put_object", Bucket=self.bucket, Key=f"{prefix}{index}/item.txt", Body=f"{index}".encode())
        keys: list[str] = []
        token: str | None = None
        pages = 0
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 2}
            if token:
                kwargs["ContinuationToken"] = token
            response = self._call("list_objects_v2", **kwargs)
            pages += 1
            keys.extend(item["Key"] for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            require(token, "ListObjectsV2 marked truncated without continuation token")
        require(len(keys) == 5 and all(item.startswith(prefix) for item in keys), "ListObjectsV2 pagination/prefix mismatch")
        return {"api": "ListObjectsV2", "pages": pages, "objects": len(keys), "prefix": prefix}

    def _list_v1_delimiter(self) -> dict[str, Any]:
        prefix = self.key("list/")
        response = self._call("list_objects", Bucket=self.bucket, Prefix=prefix, Delimiter="/", MaxKeys=2)
        common = [item.get("Prefix") for item in response.get("CommonPrefixes", [])]
        require(common or response.get("Contents"), "ListObjects delimiter returned no entries")
        return {"api": "ListObjects", "prefix": prefix, "delimiter": "/", "common_prefixes": len(common), "objects": len(response.get("Contents", []))}

    def _copy(self) -> dict[str, Any]:
        source = self.key("data/object.txt")
        target = self.key("data/copied.txt")
        response = self._call("copy_object", Bucket=self.bucket, Key=target, CopySource={"Bucket": self.bucket, "Key": source})
        got = self._call("get_object", Bucket=self.bucket, Key=target)
        size, digest = _consume_response(got)
        expected = self._expected["object"]
        require(size == len(expected) and digest == hashlib.sha256(expected).hexdigest(), "CopyObject content mismatch")
        return {"source": source, "target": target, "bytes": size, "sha256": digest, "etag": _etag(response.get("CopyObjectResult", {}).get("ETag") or response.get("ETag"))}

    def _tags(self) -> dict[str, Any]:
        key = self.key("data/tagged.txt")
        self._call("put_object", Bucket=self.bucket, Key=key, Body=b"tagged")
        tags = [{"Key": "run", "Value": self.run_id}, {"Key": "purpose", "Value": "acceptance"}]
        self._call("put_object_tagging", Bucket=self.bucket, Key=key, Tagging={"TagSet": tags})
        response = self._call("get_object_tagging", Bucket=self.bucket, Key=key)
        observed = {(item.get("Key"), item.get("Value")) for item in response.get("TagSet", [])}
        require({(item["Key"], item["Value"]) for item in tags}.issubset(observed), "object tags mismatch")
        self._call("delete_object_tagging", Bucket=self.bucket, Key=key)
        return {"key": key, "tags": len(observed), "deleted": True}

    def _acl(self) -> dict[str, Any]:
        key = self.key("data/object.txt")
        response = self._call("get_object_acl", Bucket=self.bucket, Key=key)
        result = {"key": key, "grants": len(response.get("Grants", [])), "owner_present": bool(response.get("Owner"))}
        requested = self.config["safety"].get("object_acl")
        if requested:
            if requested == "public-read" and not (self.config["safety"].get("allow_public_acl") and self.config["safety"].get("confirm_risk")):
                raise CaseSkip("public-read requires --allow-public-acl and --confirm-risk")
            self._call("put_object_acl", Bucket=self.bucket, Key=key, ACL=requested)
            result["modified"] = requested
        return result

    def _versions(self) -> dict[str, Any]:
        status = self._call("get_bucket_versioning", Bucket=self.bucket).get("Status")
        if status != "Enabled":
            raise CaseSkip("bucket versioning is disabled; version/delete-marker scenario is not applicable")
        key = self.key("data/versioned.txt")
        first = self._call("put_object", Bucket=self.bucket, Key=key, Body=b"version-1")
        second = self._call("put_object", Bucket=self.bucket, Key=key, Body=b"version-2")
        versions = self._call("list_object_versions", Bucket=self.bucket, Prefix=key)
        observed = [v for v in versions.get("Versions", []) if v.get("Key") == key]
        require(len(observed) >= 2, "version list did not contain both object versions")
        self._call("delete_object", Bucket=self.bucket, Key=key)
        after = self._call("list_object_versions", Bucket=self.bucket, Prefix=key)
        markers = [d for d in after.get("DeleteMarkers", []) if d.get("Key") == key]
        require(markers, "delete marker was not returned for versioned delete")
        return {"key": key, "status": status, "versions": len(observed), "delete_markers": len(markers), "version_ids_present": bool(first.get("VersionId") or second.get("VersionId"))}

    def _delete_one(self) -> dict[str, Any]:
        key = self.key("data/delete-one.txt")
        self._call("put_object", Bucket=self.bucket, Key=key, Body=b"delete-one")
        self._call("head_object", Bucket=self.bucket, Key=key)
        self._call("delete_object", Bucket=self.bucket, Key=key)
        try:
            self._call("head_object", Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _not_found(exc):
                return {"key": key, "deleted": True}
            raise
        raise AssertionError("DeleteObject left the object readable")

    def _delete_many(self) -> dict[str, Any]:
        keys = [self.key(f"data/batch-{i}.txt") for i in range(3)]
        for key in keys:
            self._call("put_object", Bucket=self.bucket, Key=key, Body=b"batch")
        response = self._call("delete_objects", Bucket=self.bucket, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": False})
        errors = response.get("Errors", [])
        require(not errors and len(response.get("Deleted", [])) == len(keys), "DeleteObjects returned errors or incomplete deletion")
        return {"requested": len(keys), "deleted": len(response.get("Deleted", [])), "errors": len(errors)}

    def _smoke_delete(self) -> dict[str, Any]:
        key = self.key("data/object.txt")
        self._call("delete_object", Bucket=self.bucket, Key=key)
        try:
            self._call("head_object", Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _not_found(exc):
                return {"key": key, "deleted": True}
            raise
        raise AssertionError("smoke DeleteObject left the object readable")

    def run_smoke(self) -> None:
        for name, function in (
            ("smoke.put_object", self._put_data),
            ("smoke.head_object", self._head_data),
            ("smoke.get_object", self._get_data),
            ("smoke.delete_object", self._smoke_delete),
        ):
            self.run_case(name, function)

    def run_data(self) -> None:
        cases: list[tuple[str, Callable[[], Any]]] = [
            ("data.head_bucket", lambda: (self._call("head_bucket", Bucket=self.bucket), {"bucket": self.bucket})[1]),
            ("data.put_object", self._put_data), ("data.head_object", self._head_data),
            ("data.get_object", self._get_data), ("data.overwrite", self._overwrite),
            ("data.range_download", self._range), ("data.list_objects_v2_pagination", self._list_v2),
            ("data.list_objects_v1_prefix_delimiter", self._list_v1_delimiter),
            ("data.copy_object", self._copy), ("data.object_tags", self._tags),
            ("data.object_acl", self._acl), ("data.versioning", self._versions),
            ("data.delete_object", self._delete_one), ("data.delete_objects", self._delete_many),
        ]
        for name, function in cases:
            result = self.run_case(name, function)
            if result.status == "FAIL" and name == "data.head_bucket":
                break

    @staticmethod
    def _pattern_file(char: bytes, size: int) -> Any:
        handle = tempfile.TemporaryFile()
        block = char * (1024 * 1024)
        remaining = size
        while remaining:
            piece = block if remaining >= len(block) else char * remaining
            handle.write(piece)
            remaining -= len(piece)
        handle.seek(0)
        return handle

    def _mp_create(self) -> dict[str, Any]:
        part_size = int(self.config["execution"]["multipart_part_size_mb"]) * 1024 * 1024
        require(part_size >= 5 * 1024 * 1024, "multipart part size must be at least 5 MiB")
        key = self.key("multipart/object.bin")
        source_key = self.key("multipart/source.bin")
        response = self._call("create_multipart_upload", Bucket=self.bucket, Key=key, ContentType="application/octet-stream")
        self._mp_state = {"key": key, "source_key": source_key, "upload_id": response["UploadId"], "part_size": part_size, "parts": []}
        return {"key": key, "upload_id": response["UploadId"], "part_size_bytes": part_size, "source_key": source_key}

    def _mp_upload_part(self) -> dict[str, Any]:
        state = self._mp_state
        part_file = self._pattern_file(b"A", state["part_size"])
        try:
            response = self._call("upload_part", Bucket=self.bucket, Key=state["key"], UploadId=state["upload_id"], PartNumber=1, Body=part_file)
        finally:
            part_file.close()
        state["parts"].append({"PartNumber": 1, "ETag": response["ETag"]})
        return {"part_number": 1, "bytes": state["part_size"], "etag": _etag(response["ETag"])}

    def _mp_list_parts(self) -> dict[str, Any]:
        state = self._mp_state
        response = self._call("list_parts", Bucket=self.bucket, Key=state["key"], UploadId=state["upload_id"])
        require(any(int(item.get("PartNumber", 0)) == 1 for item in response.get("Parts", [])), "ListParts lost the first part")
        return {"parts": len(response.get("Parts", [])), "upload_id": state["upload_id"]}

    def _mp_upload_part_copy(self) -> dict[str, Any]:
        state = self._mp_state
        source_file = self._pattern_file(b"C", state["part_size"])
        try:
            self._call("put_object", Bucket=self.bucket, Key=state["source_key"], Body=source_file)
        finally:
            source_file.close()
        try:
            response = self._call("upload_part_copy", Bucket=self.bucket, Key=state["key"], UploadId=state["upload_id"], PartNumber=2, CopySource={"Bucket": self.bucket, "Key": state["source_key"]})
            result = response.get("CopyPartResult", response)
            state["parts"].append({"PartNumber": 2, "ETag": result["ETag"]})
            return {"part_number": 2, "source_key": state["source_key"], "etag": _etag(result["ETag"])}
        except ClientError as exc:
            if not _unsupported(exc):
                raise
            fallback_file = self._pattern_file(b"C", state["part_size"])
            try:
                fallback = self._call("upload_part", Bucket=self.bucket, Key=state["key"], UploadId=state["upload_id"], PartNumber=2, Body=fallback_file)
            finally:
                fallback_file.close()
            state["parts"].append({"PartNumber": 2, "ETag": fallback["ETag"]})
            raise CaseWarning(f"UploadPartCopy is not supported; regular UploadPart fallback completed: {_error_message(exc)}")

    def _mp_resume(self) -> dict[str, Any]:
        state = self._mp_state
        pause = float(self.config["execution"]["multipart_pause_seconds"])
        if pause:
            time.sleep(pause)
        response = self._call("list_parts", Bucket=self.bucket, Key=state["key"], UploadId=state["upload_id"])
        require(len(response.get("Parts", [])) >= 2, "multipart resume did not retain uploaded parts")
        return {"paused_seconds": pause, "resumed": True, "parts_retained": len(response.get("Parts", []))}

    def _mp_complete(self) -> dict[str, Any]:
        state = self._mp_state
        self._call("complete_multipart_upload", Bucket=self.bucket, Key=state["key"], UploadId=state["upload_id"], MultipartUpload={"Parts": sorted(state["parts"], key=lambda item: item["PartNumber"])})
        response = self._call("get_object", Bucket=self.bucket, Key=state["key"])
        size, digest = _consume_response(response)
        expected_hash = hashlib.sha256()
        for char in (b"A", b"C"):
            remaining = state["part_size"]
            block = char * (1024 * 1024)
            while remaining:
                piece = block if remaining >= len(block) else char * remaining
                expected_hash.update(piece)
                remaining -= len(piece)
        require(size == state["part_size"] * 2 and digest == expected_hash.hexdigest(), "completed multipart content mismatch")
        return {"key": state["key"], "parts": len(state["parts"]), "bytes": size, "sha256": digest}

    def _mp_abort(self) -> dict[str, Any]:
        state = self._mp_state
        abort_key = self.key("multipart/aborted.bin")
        response = self._call("create_multipart_upload", Bucket=self.bucket, Key=abort_key)
        self._call("abort_multipart_upload", Bucket=self.bucket, Key=abort_key, UploadId=response["UploadId"])
        uploads = self._call("list_multipart_uploads", Bucket=self.bucket, Prefix=self.prefix).get("Uploads", [])
        require(not any(item.get("UploadId") == response["UploadId"] for item in uploads), "AbortMultipartUpload left an upload")
        return {"key": abort_key, "upload_id": response["UploadId"], "aborted": True}

    def run_multipart(self) -> None:
        self._mp_state = None
        cases = [
            ("multipart.CreateMultipartUpload", self._mp_create),
            ("multipart.UploadPart", self._mp_upload_part),
            ("multipart.ListParts", self._mp_list_parts),
            ("multipart.UploadPartCopy", self._mp_upload_part_copy),
            ("multipart.PauseResume", self._mp_resume),
            ("multipart.CompleteMultipartUpload", self._mp_complete),
            ("multipart.AbortMultipartUpload", self._mp_abort),
        ]
        for name, function in cases:
            if self._mp_state is None and name != "multipart.CreateMultipartUpload":
                self.run_case(name, lambda: (_ for _ in ()).throw(CaseSkip("CreateMultipartUpload did not complete")))
                continue
            result = self.run_case(name, function)
            if result.status == "FAIL" and name == "multipart.CreateMultipartUpload":
                self._mp_state = None

    def run_performance(self) -> None:
        count = int(self.config["execution"]["performance_objects"])
        size = int(self.config["execution"]["performance_object_size_kb"]) * 1024
        payload = b"P" * size
        def measure() -> dict[str, Any]:
            started = time.perf_counter()
            def put(index: int) -> None:
                self._call("put_object", Bucket=self.bucket, Key=self.key(f"performance/{index}.bin"), Body=payload)
            with ThreadPoolExecutor(max_workers=int(self.config["execution"]["concurrency"])) as executor:
                list(executor.map(put, range(count)))
            elapsed = time.perf_counter() - started
            throughput = count / elapsed if elapsed else 0.0
            return {"objects": count, "bytes": count * size, "objects_per_second": round(throughput, 2), "concurrency": self.config["execution"]["concurrency"]}
        self.run_case("performance.put_objects", measure)

    def run_security(self) -> None:
        def transport() -> dict[str, Any]:
            if urlparse(self.endpoint).scheme != "https":
                raise CaseWarning("endpoint is HTTP; use HTTPS for production credentials")
            if not self.config["connection"].get("verify_tls"):
                raise CaseWarning("HTTPS certificate verification is disabled")
            return {"https": True, "verify_tls": True}
        self.run_case("security.transport", transport)

    def run_control_plane(self) -> None:
        safety = self.config["safety"]
        if not safety.get("confirm_control_plane"):
            self.run_case("control-plane.guard", lambda: (_ for _ in ()).throw(CaseSkip("control-plane requires --confirm-control-plane on an explicitly supplied dedicated bucket")))
            return
        original: dict[str, Any] = {}
        changes: list[str] = []
        self.control_plane_state["snapshots"] = []
        def snapshot(name: str, getter: str, not_found_ok: bool = True) -> None:
            try:
                original[name] = self._call(getter, Bucket=self.bucket)
            except Exception as exc:
                if not_found_ok and _not_found(exc):
                    original[name] = None
                elif _unsupported(exc):
                    raise CaseWarning(f"{name} is not supported: {_error_message(exc)}")
                else:
                    raise
        snapshot_results = []
        for name, getter in (("acl", "get_bucket_acl"), ("policy", "get_bucket_policy"), ("versioning", "get_bucket_versioning"), ("lifecycle", "get_bucket_lifecycle_configuration"), ("encryption", "get_bucket_encryption"), ("cors", "get_bucket_cors"), ("tagging", "get_bucket_tagging")):
            result = self.run_case(f"control-plane.snapshot.{name}", lambda n=name, g=getter: snapshot(n, g))
            snapshot_results.append(result)
            self.control_plane_state["snapshots"].append(name)
        if any(result.status != "PASS" for result in snapshot_results):
            self.run_case("control-plane.guard", lambda: (_ for _ in ()).throw(CaseSkip("control-plane snapshot incomplete; no configuration was modified")))
            return

        def control_acl() -> dict[str, Any]:
            self._call("put_bucket_acl", Bucket=self.bucket, ACL="private")
            changes.append("acl")
            observed = self._call("get_bucket_acl", Bucket=self.bucket)
            require(observed.get("Owner") is not None, "Bucket ACL readback has no owner")
            return {"acl": "private", "grants": len(observed.get("Grants", []))}

        def control_policy() -> dict[str, Any]:
            if not (safety.get("allow_public_policy") and safety.get("confirm_risk")):
                raise CaseSkip("public-read bucket Policy is disabled by default; require --allow-public-policy --confirm-risk")
            policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{self.bucket}/*"}]}
            self._call("put_bucket_policy", Bucket=self.bucket, Policy=json.dumps(policy))
            changes.append("policy")
            observed = self._call("get_bucket_policy", Bucket=self.bucket)
            require(observed.get("Policy"), "Bucket Policy readback is empty")
            return {"public_policy": True}

        def control_versioning() -> dict[str, Any]:
            status = (original.get("versioning") or {}).get("Status")
            if status not in {"Enabled", "Suspended"}:
                raise CaseSkip("enabling versioning on an unversioned bucket is not reversible")
            self._call("put_bucket_versioning", Bucket=self.bucket, VersioningConfiguration={"Status": status})
            changes.append("versioning")
            return {"status": self._call("get_bucket_versioning", Bucket=self.bucket).get("Status")}

        def control_lifecycle() -> dict[str, Any]:
            value = {"Rules": [{"ID": f"oss-tester-{self.run_id}", "Status": "Enabled", "Filter": {"Prefix": self.prefix}, "Expiration": {"Days": 365}}]}
            self._call("put_bucket_lifecycle_configuration", Bucket=self.bucket, LifecycleConfiguration=value)
            changes.append("lifecycle")
            return {"rules": len(self._call("get_bucket_lifecycle_configuration", Bucket=self.bucket).get("Rules", []))}

        def control_encryption() -> dict[str, Any]:
            value = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
            self._call("put_bucket_encryption", Bucket=self.bucket, ServerSideEncryptionConfiguration=value)
            changes.append("encryption")
            return {"algorithm": self._call("get_bucket_encryption", Bucket=self.bucket).get("ServerSideEncryptionConfiguration", value).get("Rules", [{}])[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")}

        def control_cors() -> dict[str, Any]:
            value = {"CORSRules": [{"AllowedHeaders": ["*"], "AllowedMethods": ["GET", "HEAD"], "AllowedOrigins": ["https://example.invalid"]}]}
            self._call("put_bucket_cors", Bucket=self.bucket, CORSConfiguration=value)
            changes.append("cors")
            return {"rules": len(self._call("get_bucket_cors", Bucket=self.bucket).get("CORSRules", []))}

        def restore() -> dict[str, Any]:
            errors: list[str] = []
            for name, action in (("policy", self._restore_policy), ("lifecycle", self._restore_lifecycle), ("encryption", self._restore_encryption), ("cors", self._restore_cors), ("tagging", self._restore_tagging), ("acl", self._restore_acl), ("versioning", self._restore_versioning)):
                if name not in original:
                    continue
                try:
                    action(original[name])
                except Exception as exc:
                    errors.append(f"{name}: {_error_message(exc)}")
            if errors:
                raise CaseWarning("configuration restore incomplete: " + "; ".join(errors))
            self.control_plane_state["restored"] = list(original)
            return {"restored": list(original), "changes": changes}
        try:
            for name, function in (("acl", control_acl), ("policy", control_policy), ("versioning", control_versioning), ("lifecycle", control_lifecycle), ("encryption", control_encryption), ("cors", control_cors), ("tagging", lambda: self._control_tagging(original, changes))):
                self.run_case(f"control-plane.{name}", function)
        finally:
            self.control_plane_state["changes"] = changes
            self.run_case("control-plane.restore", restore)

    def _control_tagging(self, original: dict[str, Any], changes: list[str]) -> dict[str, Any]:
        value = {"TagSet": [{"Key": "oss-tester", "Value": self.run_id}]}
        self._call("put_bucket_tagging", Bucket=self.bucket, Tagging=value)
        changes.append("tagging")
        observed = self._call("get_bucket_tagging", Bucket=self.bucket)
        require(any(item.get("Key") == "oss-tester" for item in observed.get("TagSet", [])), "bucket tagging mismatch")
        return {"tagging": True}

    def _restore_policy(self, value: Any) -> None:
        if value is None:
            try: self._call("delete_bucket_policy", Bucket=self.bucket)
            except Exception as exc:
                if not _not_found(exc): raise
        else:
            self._call("put_bucket_policy", Bucket=self.bucket, Policy=value.get("Policy", value))

    def _restore_lifecycle(self, value: Any) -> None:
        if value is None: self._call("delete_bucket_lifecycle", Bucket=self.bucket)
        else: self._call("put_bucket_lifecycle_configuration", Bucket=self.bucket, LifecycleConfiguration=_api_payload(value))

    def _restore_encryption(self, value: Any) -> None:
        if value is None:
            try: self._call("delete_bucket_encryption", Bucket=self.bucket)
            except Exception as exc:
                if not _not_found(exc): raise
        else:
            clean = _api_payload(value)
            self._call("put_bucket_encryption", Bucket=self.bucket, ServerSideEncryptionConfiguration=clean.get("ServerSideEncryptionConfiguration", clean))

    def _restore_cors(self, value: Any) -> None:
        if value is None: self._call("delete_bucket_cors", Bucket=self.bucket)
        else: self._call("put_bucket_cors", Bucket=self.bucket, CORSConfiguration=_api_payload(value))

    def _restore_tagging(self, value: Any) -> None:
        if value is None:
            try: self._call("delete_bucket_tagging", Bucket=self.bucket)
            except Exception as exc:
                if not _not_found(exc): raise
        else: self._call("put_bucket_tagging", Bucket=self.bucket, Tagging=_api_payload(value))

    def _restore_acl(self, value: Any) -> None:
        if value is not None:
            policy = {key: value[key] for key in ("Owner", "Grants") if key in value}
            self._call("put_bucket_acl", Bucket=self.bucket, AccessControlPolicy=policy)

    def _restore_versioning(self, value: Any) -> None:
        status = (_api_payload(value) or {}).get("Status")
        if status in {"Enabled", "Suspended"}:
            self._call("put_bucket_versioning", Bucket=self.bucket, VersioningConfiguration={"Status": status})

    def cleanup(self, remove_objects: bool = True) -> dict[str, Any]:
        """Delete only this run's prefix; abort this run's unfinished uploads always."""
        details = {"status": "PASS", "objects": 0, "versions": 0, "delete_markers": 0, "multipart_uploads": 0, "errors": []}
        try:
            while True:
                response = self._call("list_multipart_uploads", Bucket=self.bucket, Prefix=self.prefix, MaxUploads=1000)
                returned_uploads = response.get("Uploads", [])
                require(all(item.get("Key", "").startswith(self.prefix) for item in returned_uploads), "ListMultipartUploads returned an upload outside the current prefix")
                uploads = returned_uploads
                if not uploads:
                    break
                for upload in uploads:
                    self._call("abort_multipart_upload", Bucket=self.bucket, Key=upload["Key"], UploadId=upload["UploadId"])
                    details["multipart_uploads"] += 1
        except Exception as exc:
            details["status"] = "WARN" if _unsupported(exc) else "FAIL"
            details["errors"].append(_safe_name(f"multipart: {_error_message(exc)}"))

        if remove_objects:
            try:
                while True:
                    response = self._call("list_object_versions", Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1000)
                    returned_versions = response.get("Versions", [])
                    returned_markers = response.get("DeleteMarkers", [])
                    require(all(item.get("Key", "").startswith(self.prefix) for item in returned_versions + returned_markers), "ListObjectVersions returned an entry outside the current prefix")
                    versions = [item for item in returned_versions if item.get("VersionId")]
                    markers = [item for item in returned_markers if item.get("VersionId")]
                    entries = [{"Key": item["Key"], "VersionId": item["VersionId"]} for item in versions + markers]
                    if not entries: break
                    deleted = self._call("delete_objects", Bucket=self.bucket, Delete={"Objects": entries, "Quiet": False})
                    if deleted.get("Errors"):
                        raise AssertionError(f"version cleanup returned {len(deleted['Errors'])} error(s)")
                    details["versions"] += len(versions)
                    details["delete_markers"] += len(markers)
            except Exception as exc:
                if details["status"] != "FAIL":
                    details["status"] = "WARN" if _unsupported(exc) else "FAIL"
                details["errors"].append(_safe_name(f"versions: {_error_message(exc)}"))

            try:
                while True:
                    response = self._call("list_objects_v2", Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1000)
                    returned_objects = response.get("Contents", [])
                    require(all(item.get("Key", "").startswith(self.prefix) for item in returned_objects), "ListObjectsV2 returned an object outside the current prefix")
                    keys = [item["Key"] for item in returned_objects]
                    if not keys:
                        break
                    deleted = self._call("delete_objects", Bucket=self.bucket, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": False})
                    if deleted.get("Errors"):
                        raise AssertionError(f"object cleanup returned {len(deleted['Errors'])} error(s)")
                    details["objects"] += len(deleted.get("Deleted", keys))
                leftovers = self._call("list_objects_v2", Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1).get("Contents", [])
                if leftovers: raise AssertionError(f"cleanup left {len(leftovers)} object(s) under current prefix")
            except Exception as exc:
                details["status"] = "FAIL"
                details["errors"].append(_safe_name(f"objects: {_error_message(exc)}"))
        elif details["status"] == "PASS":
            details["status"] = "SKIP"
        self.cleanup_result = details
        return details

    def run_cleanup_case(self, remove_objects: bool) -> TestResult:
        started = time.perf_counter()
        try:
            details = self.cleanup(remove_objects=remove_objects)
            status = str(details["status"])
            error = "; ".join(details["errors"]) or (
                "completed objects retained by cleanup policy" if status == "SKIP" else None
            )
        except Exception as exc:
            details = self.cleanup_result
            status = "FAIL"
            error = _safe_name(_error_message(exc))
            details["status"] = "FAIL"
            details["errors"].append(error)
        result = TestResult(
            "cleanup", status, round((time.perf_counter() - started) * 1000, 2),
            _safe_name(error) if error else None, copy.deepcopy(details),
        )
        self.results.append(result)
        print(f"[{status:<4}] {'cleanup':<34} {result.duration_ms:>9.2f} ms  {error or json.dumps(details, ensure_ascii=False, separators=(',', ':'))}")
        return result


class _BodyContext:
    def __init__(self, body: Any): self.body = body
    def __enter__(self): return self.body
    def __exit__(self, *args):
        close = getattr(self.body, "close", None)
        if close: close()


def raise_warning(message: str) -> None:
    raise CaseWarning(message)


def create_s3_client(config: dict[str, Any]) -> Any:
    if boto3 is None or Config is None:
        raise RuntimeError("boto3 is not installed; run python -m pip install -r requirements.txt")
    connection = config["connection"]
    profile = connection.get("credential_profile")
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "endpoint_url": str(connection["endpoint"]).rstrip("/"),
        "region_name": connection.get("region") or "us-east-1",
        "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=float(connection["timeout"]), read_timeout=float(connection["timeout"]), retries={"max_attempts": int(connection["retry_attempts"]), "mode": "standard"}, max_pool_connections=int(config["execution"]["concurrency"])),
        "verify": bool(connection.get("verify_tls", True)),
    }
    # boto3 does not know the historical OSS_* names; support them without ever
    # copying the values into config, result objects, logs or reports.
    access_key = os.getenv("OSS_ACCESS_KEY_ID")
    secret_key = os.getenv("OSS_SECRET_ACCESS_KEY")
    if access_key and secret_key:
        kwargs.update(aws_access_key_id=access_key, aws_secret_access_key=secret_key)
    return session.client(**kwargs)


def credentials_present() -> bool:
    return bool((os.getenv("OSS_ACCESS_KEY_ID") and os.getenv("OSS_SECRET_ACCESS_KEY")) or (os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")))


def sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    safe = copy.deepcopy(config)
    safe.setdefault("connection", {})
    safe["connection"].pop("access_key_id", None)
    safe["connection"].pop("secret_access_key", None)
    safe["connection"]["credentials_present"] = credentials_present()
    return safe


def resolve_report_path(config: dict[str, Any], override: str | None, run_id: str) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    report_dir = PROJECT_ROOT / str(config["report"]["directory"])
    return report_dir / f"oss-test-{run_id}.json"


def write_report(runner: OSSRunner, config: dict[str, Any], selected_suites: list[str], started_at: str, *, report: str | None = None, exit_code: int = 0, interrupted: bool = False, interruption_reason: str | None = None, duration_ms: float | None = None) -> Path:
    summary = {status: sum(1 for result in runner.results if result.status == status) for status in STATUSES}
    overall = "FAIL" if summary["FAIL"] else ("WARN" if summary["WARN"] else ("SKIP" if summary["PASS"] == 0 else "PASS"))
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": "oss-tester",
        "run_id": runner.run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
        "endpoint": runner.endpoint,
        "region": runner.region,
        "bucket": runner.bucket,
        "profile": config["execution"]["profile"],
        "suites": selected_suites,
        "test_prefix": runner.prefix,
        "config": sanitize_config(config),
        "results": [asdict(result) for result in runner.results],
        "control_plane": runner.control_plane_state,
        "cleanup": runner.cleanup_result,
        "summary": summary,
        "overall_status": overall,
        "exit_code": exit_code,
        "interrupted": interrupted,
        "interruption_reason": interruption_reason,
        "environment": {"hostname": socket.gethostname(), "python_version": platform.python_version(), "platform": platform.platform()},
    }
    path = resolve_report_path(config, report, runner.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        try: temp_path.unlink(missing_ok=True)
        except OSError: pass
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe, repeatable S3-compatible OSS data-plane test runner", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--config", help="JSON config file (non-secret options only)")
    parser.add_argument("--endpoint")
    parser.add_argument("--region")
    parser.add_argument("--bucket")
    parser.add_argument("--credential-profile", help="boto3 shared credential profile; --profile selects test profile")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=None)
    parser.add_argument("--suites", help="comma-separated suites; overrides profile")
    parser.add_argument("--prefix", "--namespace", dest="prefix", help="base for the unique per-run object prefix")
    parser.add_argument("--report", help="JSON report path; default is reports/oss-test-RUN_ID.json")
    parser.add_argument("--cleanup", choices=["always", "on-success", "never"])
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--retry-attempts", type=int)
    parser.add_argument("--retry-backoff", type=float, dest="retry_backoff")
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--set", dest="config_overrides", action="append", default=[], metavar="SECTION.OPTION=VALUE")
    parser.add_argument("--confirm-bucket", action="store_true", help="confirm the explicitly supplied bucket is a dedicated test bucket")
    parser.add_argument("--confirm-control-plane", action="store_true", help="allow reversible control-plane mutations after snapshot")
    parser.add_argument("--confirm-risk", action="store_true", help="confirm high-risk ACL/policy actions")
    parser.add_argument("--allow-public-acl", action="store_true")
    parser.add_argument("--allow-public-policy", action="store_true")
    parser.add_argument("--object-acl", choices=["private", "public-read"], help="optional object ACL change; public-read requires confirmations")
    parser.add_argument("--list-suites", action="store_true")
    return parser


def print_suites() -> None:
    print("Profiles:")
    for name, suites in PROFILES.items(): print(f"  {name:<14} {','.join(suites)}")
    print("\nSuites:")
    for name in sorted(AVAILABLE_SUITES): print(f"  {name}")


def _suspicious_bucket(bucket: str) -> bool:
    return bool(
        re.search(r"production|business|customer", bucket, re.I)
        or re.search(r"(^|[-.])(prod|prd|live|default)([-.]|$)", bucket, re.I)
    )


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_suites:
        print_suites(); return 0
    bucket_looks_risky = False
    try:
        config = load_config(args.config)
        apply_cli_overrides(config, args)
        if args.object_acl:
            config["safety"]["object_acl"] = args.object_acl
        # object_acl is intentionally not part of the generic config allow-list;
        # it is a CLI-only operation request.
        validate_config(config, require_target=True)
        selected = choose_suites(config, args.suites)
        if "control-plane" in selected and (not args.bucket or not args.confirm_control_plane):
            raise ValueError("control-plane requires explicit --bucket and --confirm-control-plane")
        if config["safety"].get("allow_public_policy") and not (args.allow_public_policy and args.confirm_risk):
            raise ValueError("public Policy requires explicit --allow-public-policy and --confirm-risk")
        if config["safety"].get("object_acl") == "public-read" and not (args.allow_public_acl and args.confirm_risk):
            raise ValueError("public-read object ACL requires explicit --allow-public-acl and --confirm-risk")
        bucket = str(config["connection"]["bucket"])
        bucket_looks_risky = _suspicious_bucket(bucket)
        if bucket_looks_risky and not args.confirm_bucket:
            raise ValueError("bucket name looks like a business/production bucket; provide --confirm-bucket only for a dedicated test bucket")
        if not config["safety"].get("confirm_bucket") and args.bucket is None and os.getenv("OSS_BUCKET") is None:
            raise ValueError("bucket must be explicitly supplied with --bucket or OSS_BUCKET")
    except ValueError as exc:
        parser.error(str(exc)); return 2

    started = utc_now(); started_tick = time.perf_counter()
    runner = OSSRunner(config, client=client)
    interrupted = False; interruption_reason = None; exit_code = 0
    previous_handlers: dict[int, Any] = {}
    def handle_termination(received: int, _frame: Any) -> None:
        raise RunInterrupted(received)
    for signal_name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, handle_termination)
            except (OSError, ValueError):
                pass
    print("SAFETY: this run writes only to its unique prefix in the explicitly selected dedicated test bucket.")
    if bucket_looks_risky:
        print("DANGER: the bucket name resembles a production/business bucket; --confirm-bucket was supplied.")
    elif not args.confirm_bucket:
        print("WARNING: confirm this is not a production or business bucket before continuing future runs.")
    print(f"Endpoint: {runner.endpoint}\nRegion:   {runner.region}\nBucket:   {runner.bucket}\nPrefix:   {runner.prefix}\nSuites:   {','.join(selected)}")
    try:
        for suite in selected:
            runner.run_suite(suite)
    except KeyboardInterrupt:
        interrupted = True; interruption_reason = "KeyboardInterrupt"; exit_code = 130
        runner.results.append(TestResult("execution.interrupted", "FAIL", 0.0, interruption_reason, {}))
    except RunInterrupted as exc:
        interrupted = True; interruption_reason = str(exc); exit_code = 128 + exc.signum
        runner.results.append(TestResult("execution.interrupted", "FAIL", 0.0, interruption_reason, {}))
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
        failures = any(result.status == "FAIL" for result in runner.results)
        policy = config["execution"]["cleanup"]
        should_remove = policy == "always" or (policy == "on-success" and not failures)
        runner.run_cleanup_case(remove_objects=should_remove)
        if interrupted:
            exit_code = exit_code or 130
        elif any(result.status == "FAIL" for result in runner.results):
            exit_code = 1
        duration = (time.perf_counter() - started_tick) * 1000
        try:
            report_path = write_report(runner, config, selected, started, report=args.report, exit_code=exit_code, interrupted=interrupted, interruption_reason=interruption_reason, duration_ms=duration)
        except Exception as exc:
            print(f"ERROR: Could not write JSON report: {type(exc).__name__}: {exc}", file=os.sys.stderr)
            return 2
    summary = {status: sum(1 for result in runner.results if result.status == status) for status in STATUSES}
    print("Summary: " + ", ".join(f"{status}={summary[status]}" for status in STATUSES))
    print(f"Report:  {report_path}")
    return exit_code


def build_config_from_env() -> dict[str, Any]:
    """Compatibility helper used by the old CLI wrappers."""
    config = load_config()
    validate_config(config, require_target=True)
    return config


if __name__ == "__main__":
    raise SystemExit(main())
