"""Configurações da aplicação carregadas via variáveis de ambiente (.env)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # API da Câmara
    camara_api_base_url: str = "https://dadosabertos.camara.leg.br/api/v2"
    camara_arquivos_base_url: str = "https://dadosabertos.camara.leg.br/arquivos"
    camara_legislatura_id: int = 57
    camara_m2_anos: str = "2023,2024,2025,2026"
    camara_m2_tipos: str = "PL,PLP,PEC,PDL,PRC"
    camara_cache_dir: Path = Path("cache")
    camara_itens_por_pagina: int = 100
    camara_max_concorrencia: int = 2
    camara_pausa_segundos: float = 0.3
    camara_user_agent: str = "camara-em-grafo/0.1 (projeto de estudo de dados abertos)"
    camara_timeout_segundos: float = 30.0
    camara_download_timeout_segundos: float = 180.0
    camara_max_tentativas: int = 5

    # API GraphQL (M4)
    api_max_profundidade: int = 8
    api_max_custo: int = 200
    api_max_itens_pagina: int = 50
    api_rate_limit_requisicoes: int = 60
    api_rate_limit_janela_segundos: int = 60
    api_introspeccao_habilitada: bool = True
    api_graphiql_habilitado: bool = True

    def anos_m2(self) -> list[int]:
        return [int(a.strip()) for a in self.camara_m2_anos.split(",") if a.strip()]

    def tipos_m2(self) -> set[str] | None:
        raw = [t.strip().upper() for t in self.camara_m2_tipos.split(",") if t.strip()]
        if not raw or "TODOS" in raw:
            return None
        return set(raw)


def get_settings() -> Settings:
    return Settings()
