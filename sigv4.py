"""Small, independently testable AWS Signature V4 helper.

The OSS runner uses boto3 for normal operations.  This module remains for
S3-compatible endpoints that expose an operation boto3 cannot model and for
backwards compatibility with the original HTTP helper.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _amz_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class SigV4Signer:
    def __init__(self, access_key: str, secret_key: str, region: str, service: str = "s3", clock: Any = None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.service = service
        self.clock = clock or _amz_date

    def sign(self, method: str, path: str, query: dict[str, Any] | None, headers: dict[str, str], body: bytes) -> dict[str, str]:
        query = query or {}
        incoming = {str(key).lower(): str(value).strip() for key, value in headers.items()}
        host = incoming.get("host", "")
        amz_date = self.clock()
        payload_hash = sha256_hex(body)
        canonical_headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        for key, value in incoming.items():
            if key.startswith("x-amz-") and key not in canonical_headers:
                canonical_headers[key] = value
        ordered_headers = sorted(canonical_headers.items())
        signed_headers = ";".join(key for key, _ in ordered_headers)
        canonical_header_text = "".join(f"{key}:{value}\n" for key, value in ordered_headers)
        query_items: list[tuple[str, str]] = []
        for key, value in query.items():
            if isinstance(value, (list, tuple)):
                query_items.extend((str(key), str(item)) for item in value)
            else:
                query_items.append((str(key), "" if value is None else str(value)))
        canonical_query = "&".join(
            f"{urllib.parse.quote(key, safe='-_.~')}={urllib.parse.quote(value, safe='-_.~')}"
            for key, value in sorted(query_items)
        )
        canonical_request = "\n".join([
            method.upper(), path or "/", canonical_query, canonical_header_text,
            signed_headers, payload_hash,
        ])
        scope = f"{amz_date[:8]}/{self.region}/{self.service}/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            sha256_hex(canonical_request.encode("utf-8")),
        ])
        key_date = _hmac(("AWS4" + self.secret_key).encode("utf-8"), amz_date[:8])
        key_region = _hmac(key_date, self.region)
        key_service = _hmac(key_region, self.service)
        signing_key = _hmac(key_service, "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        signed = dict(headers)
        signed.update({
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        })
        return signed


def parse_error(body: bytes, status: int) -> str:
    try:
        root = ET.fromstring(body)
        values = {element.tag.rsplit("}", 1)[-1]: (element.text or "") for element in root.iter()}
        return f"HTTP {status} {values.get('Code', '')}: {values.get('Message', '')}".strip()
    except ET.ParseError:
        return f"HTTP {status}: {body[:200]!r}"


class HttpS3Client:
    """Signed HTTP fallback. It never prints or serializes its credentials."""

    def __init__(self, endpoint: str, region: str, access_key: str, secret_key: str, timeout: float = 30.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.signer = SigV4Signer(access_key, secret_key, region)
        self.host = urllib.parse.urlparse(self.endpoint).netloc

    def request(self, method: str, path: str, query: dict[str, Any] | None = None, body: bytes = b"", extra_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        query = query or {}
        query_text = urllib.parse.urlencode(query, doseq=True)
        url = f"{self.endpoint}{path}" + (f"?{query_text}" if query_text else "")
        headers = {"host": self.host, "content-length": str(len(body))}
        headers.update(extra_headers or {})
        signed = self.signer.sign(method, path, query, headers, body)
        request = urllib.request.Request(url, data=body, method=method.upper())
        for key, value in signed.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def delete_objects(self, bucket: str, keys: list[str]) -> tuple[int, dict[str, str], bytes]:
        body = ('<?xml version="1.0" encoding="UTF-8"?><Delete><Quiet>true</Quiet>' + ''.join(f"<Object><Key>{_xml_escape(key)}</Key></Object>" for key in keys) + "</Delete>").encode()
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        return self.request("POST", f"/{urllib.parse.quote(bucket, safe='')}", {"delete": ""}, body, {"content-type": "application/xml", "content-md5": md5})


def _xml_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))
