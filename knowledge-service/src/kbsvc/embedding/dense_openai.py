"""Dense embedder against any OpenAI-compatible /v1/embeddings endpoint.

Works with OpenAI, vLLM, Xinference, Ollama's OpenAI shim, TEI, etc.
"""

from __future__ import annotations

import httpx

from ..errors import KbError


class OpenAiDenseEmbedder:
    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 32,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(timeout=timeout, headers=headers)

    def _post(self, inputs: list[str]) -> list[list[float]]:
        response = self._client.post(
            f"{self.base_url}/embeddings", json={"model": self.model, "input": inputs}
        )
        if response.status_code >= 400:
            raise KbError(
                f"embedding endpoint returned {response.status_code}",
                {"body": response.text[:500]},
            )
        payload = response.json()
        rows = sorted(payload["data"], key=lambda item: item.get("index", 0))
        return [row["embedding"] for row in rows]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            vectors.extend(self._post(batch))
        if vectors and len(vectors[0]) != self.dim:
            self.dim = len(vectors[0])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._post([text])[0]
