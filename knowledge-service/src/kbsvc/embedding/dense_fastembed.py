"""Local ONNX dense embedder via fastembed. Optional dependency: kbsvc[fastembed].

BGE-family models are trained asymmetrically: queries get an instruction prefix,
passages do not. fastembed exposes that as query_embed/passage_embed, and using
the wrong one measurably degrades retrieval, so both are wired up here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..errors import DependencyMissingError

logger = logging.getLogger(__name__)

# fastembed's legacy tar layout: {cache_dir}/fast-{model}/ holding the onnx.
_LOCAL_DIR_PREFIX = "fast-"
_ONNX_GLOB = "*.onnx"


def _local_model_path(cache_dir: str, model_name: str) -> Path | None:
    short = model_name.split("/")[-1]
    for candidate in (Path(cache_dir) / f"{_LOCAL_DIR_PREFIX}{short}", Path(cache_dir) / short):
        if candidate.is_dir() and any(candidate.glob(_ONNX_GLOB)):
            return candidate
    return None


class FastEmbedDenseEmbedder:
    name = "fastembed"

    def __init__(
        self,
        model_name: str,
        dim: int,
        batch_size: int = 32,
        cache_dir: str | None = None,
    ) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - optional extra
            raise DependencyMissingError(
                "fastembed is not installed; install kbsvc[fastembed]"
            ) from exc
        self.model_name = model_name
        self.batch_size = batch_size
        local_model_path = _local_model_path(cache_dir, model_name) if cache_dir else None
        model_kwargs: dict[str, str] = {}
        if local_model_path is not None:
            # fastembed probes the Hub on every init even when weights are already
            # cached. On a restricted network that probe hangs for minutes instead
            # of failing, so go offline once the local copy is known-good.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            model_kwargs["specific_model_path"] = str(local_model_path)
            logger.debug("using local model path %s", local_model_path)
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
            **model_kwargs,
        )
        self._has_query_embed = hasattr(self._model, "query_embed")
        self._has_passage_embed = hasattr(self._model, "passage_embed")
        self.dim = self._detect_dim(dim)
        logger.info(
            "fastembed ready: model=%s dim=%d asymmetric=%s",
            model_name,
            self.dim,
            self._has_query_embed,
        )

    def _detect_dim(self, fallback: int) -> int:
        """Trust the model over configuration - a wrong dim breaks collection creation."""
        try:
            probe = next(iter(self._model.embed(["dim probe"])))
            return len(probe)
        except Exception as exc:  # pragma: no cover - depends on optional extra
            logger.warning("could not probe embedding dim (%s), falling back to %d", exc, fallback)
            return fallback

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embed = self._model.passage_embed if self._has_passage_embed else self._model.embed
        return [
            [float(value) for value in vector]
            for vector in embed(texts, batch_size=self.batch_size)
        ]

    def embed_query(self, text: str) -> list[float]:
        if self._has_query_embed:
            vector = next(iter(self._model.query_embed([text])))
        else:  # pragma: no cover - symmetric models
            vector = next(iter(self._model.embed([text])))
        return [float(value) for value in vector]
