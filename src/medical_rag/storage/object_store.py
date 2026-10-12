from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO, Protocol


@dataclass(frozen=True, slots=True)
class ObjectRef:
    key: str
    etag: str | None = None
    size_bytes: int | None = None


class ObjectNotFound(FileNotFoundError):
    pass


class ObjectStore(Protocol):
    def put(self, key: str, content: BinaryIO, content_type: str) -> ObjectRef: ...

    def get(self, key: str) -> BinaryIO: ...

    def delete(self, key: str) -> None: ...


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, content: BinaryIO, content_type: str) -> ObjectRef:
        data = content.read()
        self._objects[key] = (data, content_type)
        return ObjectRef(key=key, size_bytes=len(data))

    def get(self, key: str) -> BinaryIO:
        try:
            data, _ = self._objects[key]
        except KeyError as exc:
            raise ObjectNotFound(key) from exc
        return BytesIO(data)

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self._objects


class S3ObjectStore:
    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        import boto3

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def put(self, key: str, content: BinaryIO, content_type: str) -> ObjectRef:
        data = content.read()
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return ObjectRef(key=key, etag=response.get("ETag"), size_bytes=len(data))

    def get(self, key: str) -> BinaryIO:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.NoSuchKey as exc:
            raise ObjectNotFound(key) from exc
        return BytesIO(response["Body"].read())

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

