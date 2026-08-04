"""S3 / MinIO object store (server profile). Requires the `s3` extra."""

from __future__ import annotations

from ..config import Settings
from ..errors import DependencyMissingError, NotFoundError


class S3ObjectStore:
    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise DependencyMissingError(
                "boto3 is required for the s3 storage backend; install kbsvc[s3]"
            ) from exc

        self._client_error = ClientError
        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            region_name=settings.s3_region,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except self._client_error:
            self._client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes, *, content_type: str = "") -> str:
        extra = {"ContentType": content_type} if content_type else {}
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return self.uri(key)

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except self._client_error as exc:
            raise NotFoundError("object not found", {"key": key}) from exc
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except self._client_error:
            return False
        return True

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"
