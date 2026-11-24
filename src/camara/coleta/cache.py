"""Cache em disco para respostas brutas da API da Câmara."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


class DiskCache:
    """Armazena respostas JSON brutas em disco organizadas por recurso."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    def _key_path(self, endpoint: str, params: dict[str, Any] | None = None) -> Path:
        clean = endpoint.strip("/").replace("/", "__") or "root"
        if params:
            sorted_items = sorted((k, str(v)) for k, v in params.items() if v is not None)
            query_str = urlencode(sorted_items)
            digest = hashlib.sha256(query_str.encode("utf-8")).hexdigest()[:12]
            filename = f"{clean}__{digest}.json"
        else:
            filename = f"{clean}.json"
        return self.base_dir / filename

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        path = self._key_path(endpoint, params)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def set(
        self,
        endpoint: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> Path:
        path = self._key_path(endpoint, params)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
        return path

    def save_bundle(self, name: str, data: Any) -> Path:
        """Salva um conjunto consolidado de dados coletados (ex.: m1_legislatura_57.json)."""
        path = self.base_dir / "bundles" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
        return path

    def load_bundle(self, name: str) -> Any:
        """Carrega um conjunto consolidado salvo previamente no cache."""
        path = self.base_dir / "bundles" / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Cache '{path}' não encontrado. Execute a coleta antes de recarregar do cache."
            )
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
