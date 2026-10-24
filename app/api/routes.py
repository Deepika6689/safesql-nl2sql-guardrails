from functools import lru_cache

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import Settings, get_settings
from app.database import create_app_engine
from app.schemas import (
    ColumnInfo,
    ExamplesResponse,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    SchemaResponse,
    TableInfo,
)
from app.security.permissions import ALLOWED_COLUMNS, TABLE_DESCRIPTIONS
from app.services.query_service import QueryExecutor, QueryService
from app.services.sql_generator import DemoGenerator, SQLGenerator
from app.services.sql_validator import SQLValidator

router = APIRouter()

_HTTP_STATUS = {
    "UNSUPPORTED_QUESTION": 400,
    "MODEL_OUTPUT_INVALID": 502,
    "LLM_UNAVAILABLE": 503,
    "LLM_TIMEOUT": 504,
    "DATABASE_UNAVAILABLE": 503,
    "QUERY_TIMEOUT": 504,
}


@lru_cache
def get_query_service() -> QueryService:
    s = get_settings()
    engine = create_app_engine(s)
    return QueryService(
        s,
        SQLGenerator(s),
        SQLValidator(s.dialect, s.max_rows, s.max_sql_chars),
        QueryExecutor(engine, s.dialect, s.max_rows, s.query_timeout_seconds),
    )


@router.get("/health", response_model=HealthResponse)
def health(svc: QueryService = Depends(get_query_service)) -> HealthResponse:
    try:
        with svc.executor.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db = "up"
    except Exception:
        db = "down"
    mode = svc.settings.llm_mode
    llm = "disabled" if mode == "demo" else ("available" if svc.generator.llm_available() else "unavailable")
    ok = db == "up" and (llm == "available" or mode in ("demo", "auto"))
    return HealthResponse(status="ok" if ok else "degraded", database=db, llm=llm, llm_mode=mode)


@router.get("/schema", response_model=SchemaResponse)
def schema() -> SchemaResponse:
    return SchemaResponse(
        tables=[
            TableInfo(
                name=t,
                description=TABLE_DESCRIPTIONS[t],
                columns=[ColumnInfo(name=c, description=d) for c, d in cols.items()],
            )
            for t, cols in ALLOWED_COLUMNS.items()
        ]
    )


@router.get("/examples", response_model=ExamplesResponse)
def examples(settings: Settings = Depends(get_settings)) -> ExamplesResponse:
    return ExamplesResponse(examples=DemoGenerator(settings.dialect).examples())


@router.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, svc: QueryService = Depends(get_query_service)):
    result = svc.run(req.question)
    code = 200
    if result.status != "ok" and result.error:
        default = 400 if result.status == "rejected" else 500
        code = _HTTP_STATUS.get(result.error.code, default)
    return JSONResponse(status_code=code, content=result.model_dump(mode="json"))
