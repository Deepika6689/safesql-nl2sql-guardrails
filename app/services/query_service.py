"""Orchestration: question -> generate -> VALIDATE -> execute -> explain.

The executor only accepts a `ValidatedSQL` object, which only SQLValidator can produce, so there
is no code path (LLM, demo fallback, or otherwise) that executes unvalidated SQL.
"""

import datetime as dt
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, OperationalError

from app.config import Settings
from app.schemas import ErrorInfo, QueryResponse
from app.services.sql_generator import GenerationError, SQLGenerator
from app.services.sql_validator import SQLValidationError, SQLValidator, ValidatedSQL

log = logging.getLogger("safesql.query")
security_log = logging.getLogger("safesql.security")


class DatabaseUnavailableError(Exception):
    pass


class QueryTimeoutError(Exception):
    pass


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v


class QueryExecutor:
    def __init__(self, engine: Engine, dialect: str, max_rows: int, timeout_seconds: float):
        self.engine = engine
        self.dialect = dialect
        self.max_rows = max_rows
        self.timeout = timeout_seconds

    def execute(self, validated: ValidatedSQL) -> ExecutionResult:
        if not isinstance(validated, ValidatedSQL):  # belt and braces
            raise TypeError("Only validated SQL can be executed")
        try:
            with self.engine.connect() as conn:
                if self.dialect == "mysql":
                    conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME={int(self.timeout * 1000)}")
                else:
                    deadline = time.monotonic() + self.timeout
                    conn.connection.driver_connection.set_progress_handler(
                        lambda: 1 if time.monotonic() > deadline else 0, 10000
                    )
                result = conn.execution_options(no_parameters=True).exec_driver_sql(validated.sql)
                columns = list(result.keys())
                fetched = result.fetchmany(self.max_rows + 1)
        except OperationalError as exc:
            msg = str(exc).lower()
            if "interrupted" in msg or "max_execution_time" in msg or "3024" in msg or "timeout" in msg:
                raise QueryTimeoutError from exc
            raise DatabaseUnavailableError from exc
        except DBAPIError as exc:
            raise DatabaseUnavailableError from exc
        truncated = len(fetched) > self.max_rows
        rows = [[_jsonable(c) for c in row] for row in fetched[: self.max_rows]]
        return ExecutionResult(columns, rows, truncated)


def explain(validated: ValidatedSQL, description: str | None) -> str:
    parts = [description] if description else []
    feats = ", ".join(validated.features) if validated.features else "a simple lookup"
    parts.append(
        f"The query reads from {', '.join(validated.tables)} using {feats}, "
        f"and is capped at {validated.limit} rows."
    )
    return " ".join(parts)


class QueryService:
    def __init__(
        self, settings: Settings, generator: SQLGenerator, validator: SQLValidator, executor: QueryExecutor
    ):
        self.settings = settings
        self.generator = generator
        self.validator = validator
        self.executor = executor

    def run(self, question: str) -> QueryResponse:
        resp = QueryResponse(question=question, status="error")
        try:
            gen = self.generator.generate(question)
        except GenerationError as exc:
            resp.error = ErrorInfo(code=exc.code, message=exc.message)
            return resp
        resp.generation_mode, resp.model, resp.generated_sql, resp.notes = (
            gen.mode,
            gen.model,
            gen.sql,
            list(gen.notes),
        )

        try:
            validated = self.validator.validate(gen.sql)
        except SQLValidationError as exc:
            security_log.warning("rejected sql mode=%s code=%s sql=%r", gen.mode, exc.code, gen.sql[:200])
            resp.status = "rejected"
            if gen.mode == "llm" and exc.code in {"SYNTAX_ERROR", "EMPTY_SQL"}:
                resp.error = ErrorInfo(
                    code="MODEL_OUTPUT_INVALID", message="The model did not return valid SQL."
                )
            else:
                resp.error = ErrorInfo(code=exc.code, message=exc.message)
            return resp

        start = time.perf_counter()
        try:
            result = self.executor.execute(validated)
        except QueryTimeoutError:
            resp.error = ErrorInfo(code="QUERY_TIMEOUT", message="The query exceeded the time limit.")
            return resp
        except DatabaseUnavailableError:
            log.error("database error while executing validated query")
            resp.error = ErrorInfo(
                code="DATABASE_UNAVAILABLE", message="The database is unavailable or the query failed."
            )
            return resp
        resp.execution_time_ms = round((time.perf_counter() - start) * 1000, 2)
        resp.status = "ok"
        resp.executed_sql = validated.sql
        resp.columns, resp.rows = result.columns, result.rows
        resp.row_count, resp.truncated = len(result.rows), result.truncated
        resp.explanation = (
            explain(validated, gen.description)
            + f" It returned {resp.row_count} row(s)"
            + (" (truncated at the row limit)." if result.truncated else ".")
        )
        return resp
