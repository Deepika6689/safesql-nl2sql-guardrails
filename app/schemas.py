"""Pydantic request/response models."""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MAX_QUESTION_CHARS = 500


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=MAX_QUESTION_CHARS)

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("question is too short")
        return v


class ErrorInfo(BaseModel):
    code: str
    message: str


class QueryResponse(BaseModel):
    question: str
    status: Literal["ok", "rejected", "error"]
    generation_mode: Literal["llm", "demo"] | None = None
    model: str | None = None
    generated_sql: str | None = None  # exactly what the generator produced (untrusted)
    executed_sql: str | None = None  # validated, canonical, row-limited SQL (only if executed)
    explanation: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    truncated: bool = False
    execution_time_ms: float | None = None
    notes: list[str] = []
    error: ErrorInfo | None = None


class ColumnInfo(BaseModel):
    name: str
    description: str


class TableInfo(BaseModel):
    name: str
    description: str
    columns: list[ColumnInfo]


class SchemaResponse(BaseModel):
    tables: list[TableInfo]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["up", "down"]
    llm: Literal["available", "unavailable", "disabled"]
    llm_mode: str


class ExamplesResponse(BaseModel):
    examples: list[str]
