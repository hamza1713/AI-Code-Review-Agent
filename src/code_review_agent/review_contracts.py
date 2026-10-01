"""Immutable review inputs and public, source-free provenance types."""

import base64
import hashlib
import io
import json
import tokenize
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from code_review_agent.path_validation import validate_diff_path


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


class FileCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: str
    status: Literal[
        "AVAILABLE",
        "MISSING",
        "LIMIT_EXCEEDED",
        "MISMATCH",
        "UNSUPPORTED",
        "FAILED",
        "DELETED",
    ]
    reason: str = ""
    sha256: str | None = None


class SourceFile(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: str
    content_b64: str = Field(repr=False)
    sha256: str
    source: Literal["HEAD_BLOB", "VERIFIED_CHECKOUT", "UPLOAD", "NEW_FILE", "CORPUS"]

    @model_validator(mode="after")
    def verify(self):
        validate_diff_path(self.path)
        raw = self.raw
        if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("Source hash or size mismatch")
        return self

    @property
    def raw(self) -> bytes:
        return base64.b64decode(self.content_b64, validate=True)

    @property
    def text(self) -> str:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(self.raw).readline)
        return self.raw.decode(encoding)

    @classmethod
    def from_bytes(
        cls,
        path: str,
        content: bytes,
        source: Literal["HEAD_BLOB", "VERIFIED_CHECKOUT", "UPLOAD", "NEW_FILE", "CORPUS"],
    ):
        return cls(
            path=path,
            content_b64=base64.b64encode(content).decode(),
            sha256=hashlib.sha256(content).hexdigest(),
            source=source,
        )


class ReviewInputSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    diff: str = Field(repr=False)
    analyzed_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    repository: str | None = None
    files: tuple[SourceFile, ...] = ()
    coverage: tuple[FileCoverage, ...] = ()

    @model_validator(mode="after")
    def bounds(self):
        from code_review_agent.diff_parser import DiffParser

        paths = [f.path for f in self.files]
        if (
            len(paths) != len({p.casefold() for p in paths})
            or len(paths) > 100
            or sum(len(f.raw) for f in self.files) > 20 * 1024 * 1024
        ):
            raise ValueError("Snapshot exceeds source limits or repeats paths")
        parsed = DiffParser.parse_diff(self.diff)
        expected = [
            f.source_file if f.is_deleted_file else f.target_file for f in parsed.files
        ]
        if (
            len(expected) != len({p.casefold() for p in expected})
            or set(expected) != {c.path for c in self.coverage}
            or len(expected) != len(self.coverage)
        ):
            raise ValueError("Snapshot coverage does not match unique changed files")
        sources = {f.path: f for f in self.files}
        if set(sources) != {c.path for c in self.coverage if c.status == "AVAILABLE"}:
            raise ValueError("Snapshot coverage contradicts available source")
        for c in self.coverage:
            validate_diff_path(c.path)
            if c.status == "AVAILABLE" and c.sha256 != sources[c.path].sha256:
                raise ValueError("Snapshot coverage hash mismatch")
        for fd in parsed.files:
            if fd.target_file not in sources:
                continue
            lines = sources[fd.target_file].text.splitlines()
            for hunk in fd.hunks:
                cursor = hunk.new_start
                for line in hunk.lines:
                    if line.startswith(("+", " ")):
                        if (
                            cursor < 1
                            or cursor > len(lines)
                            or lines[cursor - 1] != line[1:]
                        ):
                            raise ValueError("Snapshot source does not match diff")
                        cursor += 1
        return self

    @property
    def snapshot_hash(self) -> str:
        return digest(
            {
                "diff": hashlib.sha256(self.diff.encode()).hexdigest(),
                "commit": self.analyzed_commit,
                "repository": self.repository,
                "files": [(f.path, f.sha256) for f in self.files],
                "coverage": [c.model_dump() for c in self.coverage],
            }
        )


class ReviewerProfile(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: Literal[1] = 1
    application_commit: str
    model: str
    generation_settings: dict
    prompt_hashes: dict[str, str]
    scanner_versions: dict[str, str]
    configuration: dict

    @property
    def profile_id(self) -> str:
        return digest(self.model_dump())
