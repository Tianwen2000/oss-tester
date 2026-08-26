#!/usr/bin/env python3
"""Generate and seed deterministic OSS objects used by CDN data-plane tests.

The fixture command deliberately keeps the objects in the bucket. CDN tests
need stable origin content to exercise cache, range, compression, redirect,
and error-routing behavior. Every invocation uses a unique object prefix.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable


DEFAULT_FIXTURE_DIRECTORY = Path("fixtures") / "cdn"
DEFAULT_PREFIX = "cdn-test"
PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class FixtureSpec:
    """Description of one generated origin object."""

    relative_path: str
    content_type: str
    cache_control: str = "max-age=60"
    content_encoding: str | None = None
    cdn_status: int = 200


FIXTURE_SPECS: tuple[FixtureSpec, ...] = (
    FixtureSpec("small.txt", "text/plain; charset=utf-8", "max-age=60"),
    FixtureSpec("large.bin", "application/octet-stream", "max-age=300"),
    FixtureSpec("range.bin", "application/octet-stream", "max-age=60"),
    FixtureSpec("cache.txt", "text/plain; charset=utf-8", "max-age=86400"),
    FixtureSpec("gzip.txt", "text/plain; charset=utf-8", "max-age=300", "gzip"),
    FixtureSpec("redirect/301.html", "text/html; charset=utf-8", "max-age=60", cdn_status=301),
    FixtureSpec("redirect/302.html", "text/html; charset=utf-8", "max-age=60", cdn_status=302),
    FixtureSpec("redirect/307.html", "text/html; charset=utf-8", "max-age=60", cdn_status=307),
    FixtureSpec("redirect/308.html", "text/html; charset=utf-8", "max-age=60", cdn_status=308),
    FixtureSpec("errors/404.html", "text/html; charset=utf-8", "no-cache", cdn_status=404),
    FixtureSpec("errors/403.html", "text/html; charset=utf-8", "no-cache", cdn_status=403),
    FixtureSpec("errors/500.html", "text/html; charset=utf-8", "no-cache", cdn_status=500),
    FixtureSpec("errors/503.html", "text/html; charset=utf-8", "no-cache", cdn_status=503),
)


def _write_bytes(path: Path, payload: bytes, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _write_repeated(path: Path, size: int, *, seed: bytes, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    block = hashlib.sha256(seed).digest() * (1024 * 1024 // 32)
    remaining = size
    with path.open("wb") as stream:
        while remaining:
            chunk = block[: min(len(block), remaining)]
            stream.write(chunk)
            remaining -= len(chunk)


def generate_fixture_directory(directory: str | os.PathLike[str] = DEFAULT_FIXTURE_DIRECTORY, *, overwrite: bool = False) -> Path:
    """Create all standard CDN fixture files and return their directory.

    Files are deterministic and generated in bounded chunks. Existing files
    are preserved by default so a user can add provider-specific content.
    """

    root = Path(directory).expanduser()
    _write_bytes(
        root / "small.txt",
        b"oss-tester CDN fixture: small origin object.\n",
        overwrite=overwrite,
    )
    _write_repeated(root / "large.bin", 8 * 1024 * 1024, seed=b"oss-tester-large", overwrite=overwrite)
    _write_repeated(root / "range.bin", 1024 * 1024, seed=b"oss-tester-range", overwrite=overwrite)
    _write_bytes(
        root / "cache.txt",
        b"oss-tester CDN cache fixture. Change the query string and compare cache behavior.\n",
        overwrite=overwrite,
    )
    gzip_payload = (
        b"oss-tester CDN gzip fixture. The object is stored with Content-Encoding: gzip.\n"
    )
    if overwrite or not (root / "gzip.txt").exists():
        root.mkdir(parents=True, exist_ok=True)
        with (root / "gzip.txt").open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as stream:
                stream.write(gzip_payload)

    for spec in FIXTURE_SPECS:
        if spec.relative_path.startswith("redirect/"):
            code = spec.cdn_status
            _write_bytes(
                root / spec.relative_path,
                (
                    f"<!doctype html><html><body>CDN redirect fixture {code}. "
                    "Configure the CDN rule to return the redirect status.</body></html>\n"
                ).encode("utf-8"),
                overwrite=overwrite,
            )
        elif spec.relative_path.startswith("errors/"):
            code = spec.cdn_status
            _write_bytes(
                root / spec.relative_path,
                (
                    f"<!doctype html><html><body>CDN error fixture {code}. "
                    "Configure the CDN or origin rule to return this status.</body></html>\n"
                ).encode("utf-8"),
                overwrite=overwrite,
            )
    return root


def _stream_digest(stream: BinaryIO, *, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return total, digest.hexdigest()


def _validate_base_prefix(prefix: str) -> str:
    base = prefix.rstrip(":")
    if not PREFIX_PATTERN.fullmatch(base):
        raise ValueError("fixture prefix must contain only letters, digits, dots, underscores, colons, or hyphens")
    return base


def new_fixture_prefix(base: str = DEFAULT_PREFIX, *, now: datetime | None = None, token: str | None = None) -> tuple[str, str]:
    """Return ``(run_id, unique_prefix)`` for one fixture seeding run."""

    base = _validate_base_prefix(base)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{timestamp}-{token or uuid.uuid4().hex[:10]}"
    return run_id, f"{base}:{run_id}:"


def _safe_manifest_value(value: Any) -> Any:
    """Keep provider responses out of the manifest except for the ETag."""

    if isinstance(value, dict):
        return {str(key): _safe_manifest_value(item) for key, item in value.items() if key != "ResponseMetadata"}
    if isinstance(value, list):
        return [_safe_manifest_value(item) for item in value]
    return value


def _safe_error(exc: BaseException) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for env_name in (
        "OSS_ACCESS_KEY_ID", "OSS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    ):
        secret = os.getenv(env_name)
        if secret:
            message = message.replace(secret, "***")
    return message.replace("\n", " ").replace("\r", " ")[:500]


def seed_cdn_fixtures(
    client: Any,
    *,
    bucket: str,
    endpoint: str,
    region: str,
    directory: str | os.PathLike[str] = DEFAULT_FIXTURE_DIRECTORY,
    base_prefix: str = DEFAULT_PREFIX,
    manifest_path: str | os.PathLike[str] | None = None,
    confirm_bucket: bool = False,
    overwrite_fixtures: bool = False,
    run_id: str | None = None,
    progress: Callable[[str], None] | None = print,
) -> dict[str, Any]:
    """Generate and upload CDN fixtures, returning the JSON-safe manifest."""

    if not confirm_bucket:
        raise ValueError("seed-cdn-fixtures requires --confirm-bucket for a dedicated test bucket")
    root = generate_fixture_directory(directory, overwrite=overwrite_fixtures)
    actual_run_id, prefix = new_fixture_prefix(base_prefix)
    if run_id:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", run_id):
            raise ValueError("run_id contains unsupported characters")
        actual_run_id = run_id
        prefix = f"{_validate_base_prefix(base_prefix)}:{run_id}:"

    objects: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for spec in FIXTURE_SPECS:
        source = root / spec.relative_path
        key = prefix + spec.relative_path.replace(os.sep, "/")
        metadata = {"oss-tester-fixture": "cdn", "oss-tester-run": actual_run_id}
        try:
            with source.open("rb") as stream:
                response = client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=stream,
                    ContentType=spec.content_type,
                    CacheControl=spec.cache_control,
                    **({"ContentEncoding": spec.content_encoding} if spec.content_encoding else {}),
                    Metadata=metadata,
                )
            with source.open("rb") as stream:
                byte_count, sha256 = _stream_digest(stream)
            item = {
                "path": spec.relative_path,
                "key": key,
                "request_path": f"/{key}",
                "bytes": byte_count,
                "sha256": sha256,
                "etag": str(response.get("ETag", "")).strip('"'),
                "content_type": spec.content_type,
                "cache_control": spec.cache_control,
                "content_encoding": spec.content_encoding,
                "metadata": metadata,
                "origin_status": 200,
                "cdn_rule_status": spec.cdn_status,
            }
            objects.append(item)
            if progress:
                progress(f"[PASS] {spec.relative_path} -> {key} ({byte_count} bytes)")
        except Exception as exc:
            message = _safe_error(exc)
            errors.append({"path": spec.relative_path, "error": message})
            if progress:
                progress(f"[FAIL] {spec.relative_path}: {message}")

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "tool": "oss-tester",
        "purpose": "cdn-origin-fixtures",
        "run_id": actual_run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "region": region,
        "bucket": bucket,
        "prefix": prefix,
        "directory": str(root),
        "object_count": len(objects),
        "objects": objects,
        "errors": errors,
        "status": "FAIL" if errors else "PASS",
        "notes": [
            "redirect and errors files are origin objects; CDN rules must produce the listed cdn_rule_status",
            "objects are intentionally retained for subsequent CDN tests",
        ],
    }
    if manifest_path:
        destination = Path(str(manifest_path).replace("{run_id}", actual_run_id)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest["manifest_path"] = str(destination)
        destination.write_text(json.dumps(_safe_manifest_value(manifest), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return manifest
