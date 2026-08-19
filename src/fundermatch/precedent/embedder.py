"""Pinned BGE-M3 adapter for precedent vectorization."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fundermatch.precedent.schema import DecidedLoanCase


@dataclass(frozen=True, slots=True)
class BgeM3Config:
    model_id: str = "BAAI/bge-m3"
    revision: str = "5617a9f61b028005a4858fdac845db406aefb181"
    snapshot_dir: Path | None = None
    use_fp16: bool = True
    vector_size: int = 1024


@dataclass(slots=True)
class BgeM3Embedder:
    """Dense-vector subset of BGE-M3, loaded lazily for seed commands."""

    config: BgeM3Config = field(default_factory=BgeM3Config)
    _model: Any | None = field(default=None, init=False, repr=False)

    @property
    def vector_size(self) -> int:
        return self.config.vector_size

    def embed_profiles(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]:
        return self._encode([case.profile_text() for case in cases])

    def embed_comments(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]:
        return self._encode([case.comments_text() for case in cases])

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed Phase 2 application text with the same pinned dense model."""

        return self._encode(texts)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        output = self._get_model().encode(
            texts,
            batch_size=8,
            max_length=1024,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        return [list(map(float, vector)) for vector in output["dense_vecs"]]

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from FlagEmbedding import BGEM3FlagModel
            from huggingface_hub import snapshot_download
        except ImportError as error:
            raise RuntimeError(
                "BGE-M3 dependencies are missing; run `uv sync --extra retrieval`"
            ) from error
        model_path = self.config.snapshot_dir
        if model_path is None:
            model_path = Path(
                snapshot_download(
                    repo_id=self.config.model_id,
                    revision=self.config.revision,
                )
            )
        self._model = BGEM3FlagModel(str(model_path), use_fp16=self.config.use_fp16)
        return self._model
