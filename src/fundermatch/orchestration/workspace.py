"""Application-scoped storage for inputs and worker artifacts.

This store is intentionally separate from LangGraph checkpoints.  It may contain
borrower content, while checkpoints contain only compact references and hashes.
"""

from __future__ import annotations

import json
import os
import shutil
from base64 import b64decode, b64encode
from hashlib import sha256
from pathlib import Path
from typing import TypeVar

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field

from fundermatch.intake import IntakeMetadata
from fundermatch.orchestration.schema import InputReference, WorkerName

_Model = TypeVar("_Model", bound=BaseModel)


class StagedFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StagedApplication(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: IntakeMetadata
    files: tuple[StagedFile, ...] = Field(min_length=1)


class ApplicationWorkspace:
    """Durable, application-isolated worker artifact repository."""

    def __init__(
        self, root: Path, *, master_key: bytes | None = None, key_version: str = "v1"
    ) -> None:
        self.root = root.resolve()
        if master_key is not None and len(master_key) != 32:
            raise ValueError("workspace master key must be exactly 32 bytes")
        self.master_key = master_key
        self.key_version = key_version

    def stage(
        self,
        metadata: IntakeMetadata,
        files: tuple[tuple[str, bytes], ...],
        *,
        max_file_bytes: int,
        max_batch_bytes: int,
    ) -> tuple[InputReference, ...]:
        if not files:
            raise ValueError("at least one borrower PDF is required")
        if sum(len(content) for _, content in files) > max_batch_bytes:
            raise ValueError("PDF batch exceeds the configured aggregate limit")
        folder = self._folder(metadata.application_id)
        documents = folder / "documents"
        documents.mkdir(parents=True, exist_ok=True)
        staged: list[StagedFile] = []
        for filename, content in files:
            safe_name = Path(filename).name
            if safe_name != filename or not safe_name.lower().endswith(".pdf"):
                raise ValueError("all uploaded files must be safely named PDFs")
            if not content.startswith(b"%PDF-"):
                raise ValueError(f"{safe_name} is not a PDF")
            if len(content) > max_file_bytes:
                raise ValueError(f"{safe_name} exceeds the configured PDF limit")
            digest = sha256(content).hexdigest()
            target = self._document_path(metadata.application_id, safe_name)
            if target.exists() and sha256(
                self._read_bytes(target, self._document_aad(metadata.application_id, safe_name))
            ).hexdigest() != digest:
                raise ValueError(f"staged file collision for {safe_name}")
            if not target.exists():
                target.write_bytes(
                    self._write_bytes(
                        content, self._document_aad(metadata.application_id, safe_name)
                    )
                )
            staged.append(StagedFile(filename=safe_name, sha256=digest))
        request = StagedApplication(metadata=metadata, files=tuple(staged))
        self.save(metadata.application_id, "request", request)
        return tuple(
            InputReference(
                reference_type="staged_pdf",
                reference_id=item.filename,
                sha256=item.sha256,
            )
            for item in staged
        )

    def request(self, application_id: str) -> StagedApplication:
        return self.load(application_id, "request", StagedApplication)

    def read_pdf(self, application_id: str, filename: str, expected_sha256: str) -> bytes:
        safe_name = Path(filename).name
        if safe_name != filename:
            raise ValueError("unsafe staged filename")
        target = self._document_path(application_id, safe_name)
        payload = self._read_bytes(target, self._document_aad(application_id, safe_name))
        if sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("staged PDF hash mismatch")
        return payload

    def exists(self, application_id: str, name: str) -> bool:
        return self._artifact_path(application_id, name).exists()

    def save(self, application_id: str, name: str, value: BaseModel) -> str:
        target = self._artifact_path(application_id, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = value.model_dump_json(indent=2)
        digest = sha256(payload.encode("utf-8")).hexdigest()
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(
            self._write_bytes(payload.encode(), self._artifact_aad(application_id, name))
        )
        temporary.replace(target)
        return digest

    def load(self, application_id: str, name: str, model: type[_Model]) -> _Model:
        target = self._artifact_path(application_id, name)
        return model.model_validate_json(
            self._read_bytes(target, self._artifact_aad(application_id, name))
        )

    def artifact_hash(self, application_id: str, name: str) -> str:
        payload = self._artifact_path(application_id, name).read_bytes()
        return sha256(payload).hexdigest()

    def invalidate_from(self, application_id: str, worker: WorkerName) -> None:
        ordered = tuple(WorkerName)
        if worker == WorkerName.HUMAN_REVIEW:
            raise ValueError("cannot invalidate past guardrails")
        for current in ordered[ordered.index(worker) :]:
            self._artifact_path(application_id, current.value).unlink(missing_ok=True)
        if ordered.index(worker) <= ordered.index(WorkerName.FINANCIAL_ANALYSIS):
            for metric in ("annual_revenue_crore", "ebitda_margin_pct", "dscr"):
                self._artifact_path(application_id, f"financial/{metric}").unlink(missing_ok=True)

    def delete_application(self, application_id: str) -> bool:
        """Remove one resolved application folder without crossing the workspace boundary."""

        folder = self._folder(application_id)
        if not folder.exists():
            return False
        shutil.rmtree(folder)
        return True

    def rewrap_all(self, new_master_key: bytes, new_key_version: str) -> int:
        """Rewrap workspace DEKs while leaving encrypted artifact data unchanged."""

        if self.master_key is None:
            raise ValueError("workspace encryption is not enabled")
        if len(new_master_key) != 32:
            raise ValueError("new workspace master key must be exactly 32 bytes")
        changed = 0
        for application in self.root.iterdir() if self.root.exists() else ():
            if not application.is_dir():
                continue
            documents = application / "documents"
            for target in documents.glob("*.enc.json") if documents.exists() else ():
                filename = target.name.removesuffix(".enc.json")
                self._rewrap_file(
                    target,
                    self._document_aad(application.name, filename),
                    new_master_key,
                    new_key_version,
                )
                changed += 1
            artifacts = application / "agent"
            for target in artifacts.rglob("*.json") if artifacts.exists() else ():
                name = target.relative_to(artifacts).with_suffix("").as_posix()
                self._rewrap_file(
                    target,
                    self._artifact_aad(application.name, name),
                    new_master_key,
                    new_key_version,
                )
                changed += 1
        self.master_key = new_master_key
        self.key_version = new_key_version
        return changed

    def _artifact_path(self, application_id: str, name: str) -> Path:
        if not name or any(part in {"", ".", ".."} for part in Path(name).parts):
            raise ValueError("unsafe artifact name")
        return self._folder(application_id) / "agent" / f"{name}.json"

    def _document_path(self, application_id: str, filename: str) -> Path:
        suffix = ".enc.json" if self.master_key is not None else ""
        return self._folder(application_id) / "documents" / f"{filename}{suffix}"

    @staticmethod
    def _document_aad(application_id: str, filename: str) -> bytes:
        return f"document|{application_id}|{filename}".encode()

    @staticmethod
    def _artifact_aad(application_id: str, name: str) -> bytes:
        return f"artifact|{application_id}|{name}".encode()

    def _write_bytes(self, content: bytes, associated: bytes) -> bytes:
        if self.master_key is None:
            return content
        dek = AESGCM.generate_key(bit_length=256)
        data_nonce = os.urandom(12)
        key_nonce = os.urandom(12)
        return json.dumps(
            {
                "version": 1,
                "key_version": self.key_version,
                "data_nonce": b64encode(data_nonce).decode(),
                "key_nonce": b64encode(key_nonce).decode(),
                "ciphertext": b64encode(
                    AESGCM(dek).encrypt(data_nonce, content, associated)
                ).decode(),
                "wrapped_key": b64encode(
                    AESGCM(self.master_key).encrypt(key_nonce, dek, associated)
                ).decode(),
            },
            sort_keys=True,
        ).encode()

    def _read_bytes(self, target: Path, associated: bytes) -> bytes:
        content = target.read_bytes()
        if self.master_key is None:
            return content
        payload = json.loads(content)
        dek = AESGCM(self.master_key).decrypt(
            b64decode(payload["key_nonce"]),
            b64decode(payload["wrapped_key"]),
            associated,
        )
        return AESGCM(dek).decrypt(
            b64decode(payload["data_nonce"]),
            b64decode(payload["ciphertext"]),
            associated,
        )

    def _rewrap_file(
        self,
        target: Path,
        associated: bytes,
        new_master_key: bytes,
        new_key_version: str,
    ) -> None:
        if self.master_key is None:
            raise ValueError("workspace encryption is not enabled")
        resolved = target.resolve()
        if self.root not in resolved.parents:
            raise ValueError("workspace key rotation escaped configured root")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        dek = AESGCM(self.master_key).decrypt(
            b64decode(payload["key_nonce"]),
            b64decode(payload["wrapped_key"]),
            associated,
        )
        key_nonce = os.urandom(12)
        payload["key_version"] = new_key_version
        payload["key_nonce"] = b64encode(key_nonce).decode()
        payload["wrapped_key"] = b64encode(
            AESGCM(new_master_key).encrypt(key_nonce, dek, associated)
        ).decode()
        temporary = resolved.with_suffix(".rewrap")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(resolved)

    def _folder(self, application_id: str) -> Path:
        if not application_id or Path(application_id).name != application_id:
            raise ValueError("unsafe application identifier")
        folder = (self.root / application_id).resolve()
        if self.root not in folder.parents:
            raise ValueError("application storage boundary violation")
        return folder
