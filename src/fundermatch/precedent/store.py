"""Qdrant persistence for the two-vector precedent representation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models

from fundermatch.precedent.schema import DecidedLoanCase, PrecedentStatus

PROFILE_VECTOR = "profile_vec"
COMMENTS_VECTOR = "comments_vec"


class PrecedentEmbedder(Protocol):
    @property
    def vector_size(self) -> int: ...

    def embed_profiles(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]: ...

    def embed_comments(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class QdrantPrecedentConfig:
    url: str = "http://127.0.0.1:6999"
    collection: str = "fundermatch_precedents"


class QdrantPrecedentStore:
    def __init__(
        self,
        config: QdrantPrecedentConfig,
        *,
        client: QdrantClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or QdrantClient(url=config.url)

    def seed(
        self,
        cases: tuple[DecidedLoanCase, ...],
        embedder: PrecedentEmbedder,
        *,
        recreate: bool = False,
    ) -> int:
        self._ensure_collection(embedder.vector_size, recreate=recreate)
        profile_vectors = embedder.embed_profiles(cases)
        comment_vectors = embedder.embed_comments(cases)
        if len(profile_vectors) != len(cases) or len(comment_vectors) != len(cases):
            raise ValueError("embedder returned a vector count that does not match the corpus")
        points = [
            models.PointStruct(
                id=self.point_id(case.case_id),
                vector={
                    PROFILE_VECTOR: profile_vector,
                    COMMENTS_VECTOR: comment_vector,
                },
                payload=case.model_dump(mode="json"),
            )
            for case, profile_vector, comment_vector in zip(
                cases, profile_vectors, comment_vectors, strict=True
            )
        ]
        self.client.upsert(collection_name=self.config.collection, points=points, wait=True)
        return len(points)

    def write_one(
        self, case: DecidedLoanCase, embedder: PrecedentEmbedder
    ) -> DecidedLoanCase:
        """Idempotently upsert one case and verify its payload from Qdrant."""

        self.seed((case,), embedder)
        records = self.client.retrieve(
            collection_name=self.config.collection,
            ids=[self.point_id(case.case_id)],
            with_payload=True,
            with_vectors=False,
        )
        if len(records) != 1 or records[0].payload is None:
            raise RuntimeError(f"Qdrant did not confirm precedent {case.case_id!r}")
        stored = DecidedLoanCase.model_validate(records[0].payload)
        if stored != case:
            raise RuntimeError(f"Qdrant payload verification failed for {case.case_id!r}")
        return stored

    def set_lifecycle(
        self,
        case_id: str,
        *,
        expected_status: PrecedentStatus,
        status: PrecedentStatus,
    ) -> DecidedLoanCase:
        point_id = self.point_id(case_id)
        records = self.client.retrieve(
            collection_name=self.config.collection,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
        )
        if len(records) != 1 or records[0].payload is None:
            raise KeyError(f"precedent {case_id!r} not found")
        current = DecidedLoanCase.model_validate(records[0].payload)
        if current.lifecycle_status is status:
            return current
        if current.lifecycle_status is not expected_status:
            raise ValueError("precedent lifecycle changed since it was reviewed")
        self.client.set_payload(
            collection_name=self.config.collection,
            payload={"lifecycle_status": status.value},
            points=[point_id],
            wait=True,
        )
        records = self.client.retrieve(
            collection_name=self.config.collection,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
        )
        if len(records) != 1 or records[0].payload is None:
            raise RuntimeError("Qdrant did not verify precedent lifecycle update")
        changed = DecidedLoanCase.model_validate(records[0].payload)
        if changed.lifecycle_status is not status:
            raise RuntimeError("Qdrant lifecycle verification failed")
        return changed

    @staticmethod
    def point_id(case_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"fundermatch:{case_id}"))

    def _ensure_collection(self, vector_size: int, *, recreate: bool) -> None:
        exists = self.client.collection_exists(self.config.collection)
        if exists and recreate:
            self.client.delete_collection(self.config.collection)
            exists = False
        if not exists:
            self.client.create_collection(
                collection_name=self.config.collection,
                vectors_config={
                    PROFILE_VECTOR: models.VectorParams(
                        size=vector_size, distance=models.Distance.COSINE
                    ),
                    COMMENTS_VECTOR: models.VectorParams(
                        size=vector_size, distance=models.Distance.COSINE
                    ),
                },
            )
