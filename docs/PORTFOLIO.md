# Portfolio material (based only on verified functionality)

**Repo name:** `safesql-nl2sql-guardrails`
**Description:** Natural-language-to-SQL with a local LLM (Ollama) and layered SQL security guardrails: FastAPI, SQLAlchemy, SQLGlot, MySQL, Streamlit.
**Topics:** `text-to-sql` `fastapi` `sqlalchemy` `sqlglot` `mysql` `ollama` `llm` `streamlit` `sql-injection` `python` `pytest`

## Resume entry
**SafeSQL — NL-to-SQL with Security Guardrails** (Python, FastAPI, SQLAlchemy, MySQL, SQLGlot, Ollama, Streamlit, pytest)
- Built a FastAPI service that converts English questions to SQL via a locally hosted LLM (Ollama) and executes them only after multi-layer validation.
- Implemented a SQLGlot-based validator (single-statement SELECT only, table/column allowlist, function controls, forced row limit, AST-regenerated SQL) plus a SELECT-only MySQL account with column-level grants.
- Wrote 103 pytest tests covering schema constraints, injection/obfuscation attempts, malformed model output, timeouts, and proof that rejected SQL never reaches the executor.
- Delivered a Streamlit UI and idempotent database provisioning scripts; designed a labelled deterministic fallback when the LLM is unavailable.

## 30-second pitch
"SafeSQL lets you query a sales database in English. A local LLM drafts the SQL, but I treat it as untrusted: a SQLGlot validator allows only a single SELECT over approved tables and columns, caps rows, and re-generates the SQL from the parsed tree before running it as a read-only database user. Tests show destructive or out-of-scope SQL is rejected before any execution."

## Interview answers
- **Problem:** non-technical users need data access without handing an LLM write or broad read power.
- **How NL2SQL works:** prompt = system rules + only the relevant approved schema + the question; model returns SQL; I extract it (strip code fences) and validate. (`sql_generator.py`)
- **Schema to model:** keyword-based table pruning plus join bridge tables; never credentials, env vars, or the excluded `email` column.
- **Why untrusted:** models hallucinate columns and can be prompt-injected; a prompt instruction is not enforcement.
- **Validation:** parse (one statement, SELECT root) -> structural blocklist -> table allowlist -> column resolution via SQLGlot qualify -> LIMIT -> regenerate SQL -> re-verify.
- **Why DB permissions too:** parsers can have bugs or dialect differentials; column-level `GRANT SELECT` and a read-only session bound the damage independently.
- **Injection / unauthorized ops:** stacked statements, DML/DDL, `information_schema`, `INTO OUTFILE`, `SLEEP`, MySQL `/*! */` comments are all tested rejections. Parameterization covers app-written SQL only.
- **Errors/timeouts:** typed exceptions -> stable error codes -> 400/502/503/504; generic messages, no traces; MySQL `MAX_EXECUTION_TIME`.
- **Stack choices:** FastAPI (typed, auto docs), SQLAlchemy (engine/pooling/dialects), MySQL (requested primary DB), Ollama (free, local), Streamlit (fast UI).
- **Cannot safely support:** writes, arbitrary functions, cross-schema access, multi-tenant data without row-level security.
- **Production next steps:** auth + rate limits, row-level security, EXPLAIN-based cost limits, audit logging store, accuracy eval set, CI, containerization.
