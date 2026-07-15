"""Addressable compressed audit frames and active SQLite size limits."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar, Mapping

import zstandard


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class AuditLocator:
    """The exact byte range of one independent zstd frame."""

    relative_path: str
    offset: int
    compressed_length: int
    sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AuditLocator":
        return cls(
            relative_path=str(value["relative_path"]),
            offset=int(value["offset"]),
            compressed_length=int(value["compressed_length"]),
            sha256=str(value["sha256"]),
        )


class AuditArchive:
    """Append and retrieve independently compressed JSON audit records."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._compressor = zstandard.ZstdCompressor(level=3)
        self._decompressor = zstandard.ZstdDecompressor()

    @staticmethod
    def _component(value: str) -> str:
        cleaned = _SAFE_COMPONENT.sub("_", str(value).strip()).strip("._")
        return cleaned or "unknown"

    def append(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        *,
        stage: str = "audit",
    ) -> AuditLocator:
        encoded = (
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        frame = self._compressor.compress(encoded)
        relative_path = (
            f"{self._component(stage)}_{self._component(run_id)}.jsonl.zst"
        )
        path = self.root / relative_path
        with self._lock:
            offset = path.stat().st_size if path.exists() else 0
            with path.open("ab") as stream:
                stream.write(frame)
                stream.flush()
        return AuditLocator(
            relative_path=relative_path,
            offset=offset,
            compressed_length=len(frame),
            sha256=hashlib.sha256(frame).hexdigest(),
        )

    def _path_for(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("audit locator path must be relative")
        root = self.root.resolve()
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("audit locator escapes archive root") from exc
        return path

    def read(self, locator: AuditLocator | Mapping[str, Any]) -> dict[str, Any]:
        item = (
            locator
            if isinstance(locator, AuditLocator)
            else AuditLocator.from_mapping(locator)
        )
        if item.offset < 0 or item.compressed_length <= 0:
            raise ValueError("invalid audit locator byte range")
        path = self._path_for(item.relative_path)
        with path.open("rb") as stream:
            stream.seek(item.offset)
            frame = stream.read(item.compressed_length)
        if len(frame) != item.compressed_length:
            raise ValueError("audit frame is truncated")
        if hashlib.sha256(frame).hexdigest() != item.sha256:
            raise ValueError("audit frame checksum mismatch")
        decoded = self._decompressor.decompress(frame)
        value = json.loads(decoded.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("audit frame must contain a JSON object")
        return value


class StorageBudgetExceeded(RuntimeError):
    """The active SQLite database would exceed the per-book hard limit."""

    def __init__(self, active_bytes: int, active_limit: int):
        self.active_bytes = int(active_bytes)
        self.active_limit = int(active_limit)
        super().__init__(
            f"active database budget exceeded: {active_bytes} > {active_limit} bytes"
        )


@dataclass(frozen=True)
class StorageBudget:
    """Predictable active database budget derived from UTF-8 source bytes."""

    SOURCE_MULTIPLIER: ClassVar[int] = 40
    FIXED_ALLOWANCE_BYTES: ClassVar[int] = 64 * 1024**2

    source_bytes: int

    def __post_init__(self) -> None:
        if self.source_bytes < 0:
            raise ValueError("source_bytes must not be negative")

    @property
    def active_limit(self) -> int:
        return (
            self.SOURCE_MULTIPLIER * int(self.source_bytes)
            + self.FIXED_ALLOWANCE_BYTES
        )

    def check(self, active_bytes: int) -> None:
        if int(active_bytes) > self.active_limit:
            raise StorageBudgetExceeded(int(active_bytes), self.active_limit)
