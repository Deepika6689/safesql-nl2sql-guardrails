import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes import get_query_service
from app.config import Settings
from app.database import create_admin_engine, create_app_engine
from app.db_schema import metadata
from app.main import create_app
from app.seed_data import seed
from app.services.query_service import QueryExecutor, QueryService
from app.services.sql_generator import OllamaGenerator, SQLGenerator
from app.services.sql_validator import SQLValidator


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        llm_mode="demo",
        _env_file=None,
    )


@pytest.fixture
def admin_engine(settings):
    eng = create_admin_engine(settings)
    metadata.create_all(eng)
    seed(eng)
    return eng


@pytest.fixture
def app_engine(settings, admin_engine):
    return create_app_engine(settings)


@pytest.fixture
def validator(settings):
    return SQLValidator("sqlite", settings.max_rows, settings.max_sql_chars)


def make_service(settings, engine, ollama_handler=None, mode="demo") -> QueryService:
    s = settings.model_copy(update={"llm_mode": mode})
    ollama = None
    if ollama_handler is not None:
        client = httpx.Client(transport=httpx.MockTransport(ollama_handler))
        ollama = OllamaGenerator(s, client=client)
    return QueryService(
        s,
        SQLGenerator(s, ollama=ollama),
        SQLValidator(s.dialect, s.max_rows, s.max_sql_chars),
        QueryExecutor(engine, s.dialect, s.max_rows, s.query_timeout_seconds),
    )


@pytest.fixture
def service(settings, app_engine):
    return make_service(settings, app_engine)


@pytest.fixture
def client_factory(settings, app_engine):
    def _make(service=None):
        app = create_app()
        svc = service or make_service(settings, app_engine)
        app.dependency_overrides[get_query_service] = lambda: svc
        return TestClient(app, raise_server_exceptions=False)

    return _make
