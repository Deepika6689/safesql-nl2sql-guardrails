"""SQL validation: the security boundary between untrusted (LLM-generated) SQL and the database.

Layers implemented here (any failure => reject, i.e. fail closed):
  1. Size limit and rejection of MySQL executable comments (/*! ... */).
  2. Parse with sqlglot using the target dialect; exactly ONE statement must result.
  3. Root must be a SELECT (or UNION/INTERSECT/EXCEPT of SELECTs).
  4. Structural walk: block write/DDL/admin node types, SELECT ... INTO, locking reads,
     session variables, placeholders, dangerous or unknown (non-allowlisted) functions.
  5. Table allowlist: no schema-qualified names (blocks information_schema, mysql.*), every table
     must be an approved table or a CTE defined in the same query.
  6. Column allowlist: sqlglot's qualifier resolves every column against the approved
     schema; unknown columns raise, and `*` is expanded to approved columns only.
  7. A row LIMIT is enforced (added, or lowered to the configured maximum).
  8. What gets executed is the SQL *regenerated from the validated AST* (comments stripped),
     and that regenerated text is re-parsed and re-checked, so the executed text is the text we checked.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError, TokenError
from sqlglot.optimizer.qualify import qualify

from app.security.permissions import ALLOWED_COLUMNS, allowed_schema_for_validator

# Node types that must never appear anywhere in the tree.
_BLOCKED_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Drop",
        "Create",
        "Alter",
        "AlterTable",
        "TruncateTable",
        "Merge",
        "Command",
        "Grant",
        "Revoke",
        "Set",
        "Use",
        "Show",
        "Describe",
        "Pragma",
        "Into",
        "Lock",
        "Transaction",
        "Commit",
        "Rollback",
        "Copy",
        "LoadData",
        "Kill",
        "Parameter",
        "SessionParameter",
        "Placeholder",
        "Analyze",
        "Attach",
        "Detach",
        "Declare",
        "Execute",
        "Refresh",
        "Comment",
        "Cache",
        "Uncache",
    )
    if hasattr(exp, name)
)

# Typed function nodes that expose server internals or cause side effects / delays.
_BLOCKED_NODE_NAMES = {
    "CurrentVersion",
    "CurrentUser",
    "CurrentSchema",
    "CurrentCatalog",
    "SessionUser",
    "CurrentRole",
    "Sleep",
}
_BLOCKED_FUNCTION_NAMES = {
    "SLEEP",
    "BENCHMARK",
    "LOAD_FILE",
    "VERSION",
    "DATABASE",
    "SCHEMA",
    "USER",
    "CURRENT_USER",
    "SESSION_USER",
    "SYSTEM_USER",
    "CONNECTION_ID",
    "GET_LOCK",
    "RELEASE_LOCK",
    "IS_FREE_LOCK",
    "IS_USED_LOCK",
    "MASTER_POS_WAIT",
    "LAST_INSERT_ID",
    "ROW_COUNT",
    "FOUND_ROWS",
    "UUID",
    "SYS_EVAL",
    "SYS_EXEC",
    "RANDOMBLOB",
    "ZEROBLOB",
    "LOAD_EXTENSION",
    "PG_SLEEP",
    "READFILE",
    "WRITEFILE",
    "SQLITE_VERSION",
    "SQLITE_SOURCE_ID",
}
# Untyped ("Anonymous") functions are rejected unless listed here.
_ALLOWED_ANONYMOUS_FUNCTIONS = {
    "DATE_FORMAT",
    "YEAR",
    "MONTH",
    "DAY",
    "DAYOFWEEK",
    "DAYNAME",
    "MONTHNAME",
    "WEEK",
    "QUARTER",
    "DATEDIFF",
    "TIMESTAMPDIFF",
    "CURDATE",
    "NOW",
    "IFNULL",
    "NULLIF",
    "ROUND",
    "FLOOR",
    "CEIL",
    "CEILING",
    "ABS",
    "LENGTH",
    "CHAR_LENGTH",
    "LOWER",
    "UPPER",
    "TRIM",
    "CONCAT",
    "SUBSTRING",
    "STRFTIME",
    "DATE",
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
}


class SQLValidationError(Exception):
    """Raised when SQL is not acceptable. `code` is stable; `message` is safe to show users."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str  # canonical, row-limited SQL: the ONLY text that may be executed
    tables: tuple[str, ...]
    limit: int
    features: tuple[str, ...]  # e.g. ("join", "group by") used for the explanation


class SQLValidator:
    def __init__(
        self,
        dialect: str = "mysql",
        max_rows: int = 100,
        max_sql_chars: int = 2000,
        allowed_columns: dict[str, dict[str, str]] | None = None,
    ):
        self.dialect = dialect
        self.max_rows = max_rows
        self.max_sql_chars = max_sql_chars
        cols = allowed_columns if allowed_columns is not None else ALLOWED_COLUMNS
        self._tables = {t.lower() for t in cols}
        self._schema = (
            allowed_schema_for_validator()
            if allowed_columns is None
            else {t: {c: "TEXT" for c in c_} for t, c_ in cols.items()}
        )

    # ------------------------------------------------------------------ public API
    def validate(self, sql: str) -> ValidatedSQL:
        root = self._parse_single_select(sql)
        self._structural_checks(root)
        tables = self._check_tables(root)

        try:
            qualified = qualify(
                root.copy(),
                schema=self._schema,
                dialect=self.dialect,
                identify=False,
                validate_qualify_columns=True,
            )
        except OptimizeError as exc:
            raise SQLValidationError(
                "UNKNOWN_COLUMN", f"Unknown or ambiguous column/table: {_short(exc)}"
            ) from exc
        except SqlglotError as exc:
            raise SQLValidationError("CANNOT_VALIDATE", "The query could not be validated.") from exc
        except Exception as exc:  # fail closed on any parser/optimizer surprise
            raise SQLValidationError("CANNOT_VALIDATE", "The query could not be validated.") from exc

        self._structural_checks(qualified)
        self._check_tables(qualified)
        features = self._features(qualified)
        limit = self._enforce_limit(qualified)

        final_sql = qualified.sql(dialect=self.dialect, comments=False)

        # Verify the text we will execute, not the tree we built it from.
        reparsed = self._parse_single_select(final_sql)
        self._structural_checks(reparsed)
        self._check_tables(reparsed)
        return ValidatedSQL(sql=final_sql, tables=tuple(sorted(tables)), limit=limit, features=features)

    # ------------------------------------------------------------------ internals
    def _parse_single_select(self, sql: str) -> exp.Expression:
        if not isinstance(sql, str) or not sql.strip():
            raise SQLValidationError("EMPTY_SQL", "No SQL was produced.")
        if len(sql) > self.max_sql_chars:
            raise SQLValidationError("SQL_TOO_LONG", "The SQL statement is too long.")
        if "/*!" in sql:
            raise SQLValidationError("FORBIDDEN_SYNTAX", "Executable comments are not allowed.")
        try:
            statements = [s for s in sqlglot.parse(sql, read=self.dialect) if s is not None]
        except (ParseError, TokenError) as exc:
            raise SQLValidationError("SYNTAX_ERROR", "The SQL could not be parsed.") from exc
        except Exception as exc:
            raise SQLValidationError("CANNOT_VALIDATE", "The query could not be validated.") from exc
        if len(statements) == 0:
            raise SQLValidationError("EMPTY_SQL", "No SQL was produced.")
        if len(statements) > 1:
            raise SQLValidationError("MULTIPLE_STATEMENTS", "Only a single statement is allowed.")
        root = statements[0]
        if isinstance(root, exp.Subquery):
            root = root.unnest()
        if not isinstance(root, exp.Select | exp.SetOperation):
            raise SQLValidationError(
                "STATEMENT_NOT_ALLOWED",
                f"Only SELECT queries are allowed (got {type(root).__name__.upper()}).",
            )
        return root

    def _structural_checks(self, root: exp.Expression) -> None:
        for node in root.walk():
            if isinstance(node, _BLOCKED_NODES) or type(node).__name__ in _BLOCKED_NODE_NAMES:
                raise SQLValidationError(
                    "FORBIDDEN_CONSTRUCT", f"Forbidden construct: {type(node).__name__.upper()}."
                )
            if isinstance(node, exp.Func):
                if isinstance(node, exp.Anonymous):
                    name = str(node.this).upper()
                    if name in _BLOCKED_FUNCTION_NAMES or name not in _ALLOWED_ANONYMOUS_FUNCTIONS:
                        raise SQLValidationError("FORBIDDEN_FUNCTION", f"Function not allowed: {name}.")
                elif node.sql_name().upper() in _BLOCKED_FUNCTION_NAMES:
                    raise SQLValidationError(
                        "FORBIDDEN_FUNCTION", f"Function not allowed: {node.sql_name()}."
                    )

    def _check_tables(self, root: exp.Expression) -> set[str]:
        cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
        used: set[str] = set()
        for table in root.find_all(exp.Table):
            if table.args.get("db") or table.args.get("catalog"):
                raise SQLValidationError(
                    "UNAUTHORIZED_TABLE", "Schema-qualified or system tables are not allowed."
                )
            if not isinstance(table.this, exp.Identifier):
                raise SQLValidationError("UNAUTHORIZED_TABLE", "Table expressions are not allowed.")
            name = table.name.lower()
            if name in cte_names:
                continue
            if name not in self._tables:
                raise SQLValidationError("UNAUTHORIZED_TABLE", f"Table not allowed: {table.name}.")
            used.add(name)
        return used

    def _enforce_limit(self, root: exp.Expression) -> int:
        current = root.args.get("limit")
        value = None
        if current is not None:
            expr = current.args.get("expression")
            if isinstance(expr, exp.Literal) and expr.is_int:
                value = int(expr.this)
        if value is None or value > self.max_rows or value < 0:
            root.set("limit", exp.Limit(expression=exp.Literal.number(self.max_rows)))
            return self.max_rows
        return value

    @staticmethod
    def _features(root: exp.Expression) -> tuple[str, ...]:
        feats = []
        if root.find(exp.Join):
            feats.append("join")
        if root.find(exp.Group):
            feats.append("group by")
        if root.find(exp.AggFunc):
            feats.append("aggregate")
        if root.find(exp.Where):
            feats.append("filter")
        if root.find(exp.Order):
            feats.append("sort")
        if root.find(exp.CTE):
            feats.append("cte")
        if len(list(root.find_all(exp.Select))) > 1:
            feats.append("subquery")
        return tuple(feats)


def _short(exc: Exception) -> str:
    text = str(exc).splitlines()[0] if str(exc) else "validation failed"
    return text.split(" Line:")[0][:120]
