"""Turns a natural-language question into a *candidate* SQL string.

Everything returned here is UNTRUSTED. It must pass SQLValidator before it touches the database.
Two generators exist and every result is labelled with the one that produced it:
  - OllamaGenerator: genuine LLM generation through a local Ollama server (mode="llm").
  - DemoGenerator:   deterministic templates for a fixed set of documented questions
                     (mode="demo"). This is NOT an LLM and is always labelled as such.
"""

import re
from dataclasses import dataclass, field

import httpx
import sqlglot

from app.config import Settings
from app.security.permissions import (
    ALLOWED_COLUMNS,
    BUSINESS_RULES,
    TABLE_DESCRIPTIONS,
)


class GenerationError(Exception):
    code = "GENERATION_FAILED"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LLMUnavailableError(GenerationError):
    code = "LLM_UNAVAILABLE"


class LLMTimeoutError(GenerationError):
    code = "LLM_TIMEOUT"


class LLMOutputError(GenerationError):
    code = "MODEL_OUTPUT_INVALID"


class UnsupportedQuestionError(GenerationError):
    code = "UNSUPPORTED_QUESTION"


@dataclass
class GeneratedSQL:
    sql: str
    mode: str  # "llm" | "demo"
    model: str | None = None
    description: str | None = None  # demo templates only: human description of the intent
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- schema selection
_TABLE_KEYWORDS = {
    "customers": ("customer", "city", "cities", "client", "buyer", "signup", "signed up", "spent", "spend"),
    "products": ("product", "stock", "categor", "price", "item", "inventory", "selling", "sold"),
    "orders": (
        "order",
        "revenue",
        "status",
        "pending",
        "completed",
        "cancelled",
        "month",
        "sales",
        "spent",
        "spend",
    ),
    "order_items": ("quantity", "revenue", "selling", "sold", "ordered", "units", "spent", "spend"),
}


def select_relevant_tables(question: str) -> list[str]:
    """Keyword-based schema pruning so the model sees only what the question needs.

    Deliberately simple: if nothing matches we send every approved table. Bridge tables
    are added so joins stay possible (products<->orders go through order_items).
    """
    q = question.lower()
    chosen = {t for t, kws in _TABLE_KEYWORDS.items() if any(k in q for k in kws)}
    if "products" in chosen and ({"orders", "customers"} & chosen):
        chosen |= {"order_items", "orders"}
    if "customers" in chosen and "order_items" in chosen:
        chosen.add("orders")
    if "order_items" in chosen and "orders" not in chosen:
        chosen.add("orders")
    if not chosen:
        chosen = set(ALLOWED_COLUMNS)
    return [t for t in ALLOWED_COLUMNS if t in chosen]  # stable order


def build_prompt(question: str, dialect: str) -> tuple[str, str]:
    """Return (system, user). Contains ONLY approved schema text: no credentials/env data."""
    tables = select_relevant_tables(question)
    schema_lines = []
    for t in tables:
        schema_lines.append(f"TABLE {t}  -- {TABLE_DESCRIPTIONS[t]}")
        for col, desc in ALLOWED_COLUMNS[t].items():
            schema_lines.append(f"  {col}: {desc}")
    name = "MySQL" if dialect == "mysql" else "SQLite"
    system = (
        f"You convert a question into exactly ONE read-only {name} SELECT statement.\n"
        "Rules:\n"
        "- Output only the SQL. No explanation, no markdown.\n"
        "- Use only the tables and columns listed below. Do not invent columns.\n"
        "- Never write INSERT, UPDATE, DELETE, DDL, or more than one statement.\n"
        + "".join(f"- {r}\n" for r in BUSINESS_RULES)
        + "\nSCHEMA:\n"
        + "\n".join(schema_lines)
    )
    return system, question.strip()


_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Pull SQL out of a model reply. Does NOT judge safety; the validator does that."""
    if not isinstance(text, str) or not text.strip():
        raise LLMOutputError("The model returned an empty response.")
    m = _FENCE.search(text)
    sql = (m.group(1) if m else text).strip()
    if not sql:
        raise LLMOutputError("The model returned an empty response.")
    return sql


class OllamaGenerator:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        self._client = client

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=self.s.llm_timeout_seconds)

    def is_available(self) -> bool:
        try:
            r = self._http().get(f"{self.s.ollama_host}/api/tags", timeout=1.5)
            if r.status_code != 200:
                return False
            names = [m.get("name", "") for m in r.json().get("models", [])]
            base = self.s.ollama_model
            return any(n == base or n.split(":")[0] == base for n in names)
        except (httpx.HTTPError, ValueError):
            return False

    def generate(self, question: str) -> GeneratedSQL:
        system, user = build_prompt(question, self.s.dialect)
        payload = {
            "model": self.s.ollama_model,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 400},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            resp = self._http().post(f"{self.s.ollama_host}/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("The local language model took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                "The local language model (Ollama) is not reachable. Is `ollama serve` running?"
            ) from exc
        if resp.status_code == 404:
            raise LLMUnavailableError(
                f"Model '{self.s.ollama_model}' is not downloaded. Run: ollama pull {self.s.ollama_model}"
            )
        if resp.status_code != 200:
            raise LLMUnavailableError("The local language model returned an error.")
        try:
            content = resp.json()["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMOutputError("The model response was malformed.") from exc
        return GeneratedSQL(sql=extract_sql(content), mode="llm", model=self.s.ollama_model)


# --------------------------------------------------------------------------- demo mode
_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twenty": 20,
}
_REVENUE = "SUM(oi.quantity * oi.unit_price)"


def _number(q: str, default: int) -> int:
    m = re.search(r"\b(\d{1,3})\b", q)
    if m:
        return max(1, min(int(m.group(1)), 100))
    for w, n in _WORD_NUMBERS.items():
        if re.search(rf"\b{w}\b", q):
            return n
    return default


def _status(q: str) -> str:
    for st in ("pending", "cancelled", "canceled", "completed"):
        if st in q:
            return "cancelled" if st == "canceled" else st
    return "completed"


# (id, example question, required regex patterns, sql builder(question) -> (sql, description))
def _templates():
    j = (
        "FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
        "JOIN order_items oi ON oi.order_id = o.order_id "
    )
    return [
        (
            "cities",
            "Which cities have the most customers?",
            [r"\bcit(y|ies)\b", r"customer"],
            lambda q: (
                "SELECT city, COUNT(*) AS customer_count FROM customers GROUP BY city "
                "ORDER BY customer_count DESC, city",
                "Counts customers per city, most first.",
            ),
        ),
        (
            "never_ordered",
            "Which customers have never placed an order?",
            [r"customer", r"never", r"order"],
            lambda q: (
                "SELECT c.customer_id, c.name, c.city FROM customers c "
                "LEFT JOIN orders o ON o.customer_id = c.customer_id WHERE o.order_id IS NULL "
                "ORDER BY c.customer_id",
                "Customers with no row in orders (any status).",
            ),
        ),
        (
            "top_customers",
            "Show the five customers who spent the most.",
            [r"customer", r"(spent|spend|spending)"],
            lambda q: (
                f"SELECT c.customer_id, c.name, c.city, {_REVENUE} AS total_spent {j}"
                f"WHERE o.status = 'completed' GROUP BY c.customer_id, c.name, c.city "
                f"ORDER BY total_spent DESC LIMIT {_number(q, 5)}",
                "Top customers by spending on completed orders.",
            ),
        ),
        (
            "low_stock",
            "Which products have fewer than ten units in stock?",
            [r"product", r"stock", r"(fewer|less|below|under|low)"],
            lambda q: (
                f"SELECT product_id, product_name, category, stock_quantity FROM products "
                f"WHERE stock_quantity < {_number(q, 10)} ORDER BY stock_quantity, product_name",
                "Products whose stock is below the threshold.",
            ),
        ),
        (
            "revenue_by_month",
            "What was the total revenue from completed orders by month?",
            [r"revenue", r"month"],
            lambda q: (
                f"SELECT DATE_FORMAT(o.order_date, '%Y-%m') AS month, {_REVENUE} AS revenue "
                f"FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = 'completed' GROUP BY DATE_FORMAT(o.order_date, '%Y-%m') "
                f"ORDER BY month",
                "Completed-order revenue grouped by calendar month.",
            ),
        ),
        (
            "revenue_by_category",
            "What is the revenue by product category?",
            [r"revenue", r"categor"],
            lambda q: (
                f"SELECT p.category, {_REVENUE} AS revenue FROM products p "
                f"JOIN order_items oi ON oi.product_id = p.product_id "
                f"JOIN orders o ON o.order_id = oi.order_id WHERE o.status = 'completed' "
                f"GROUP BY p.category ORDER BY revenue DESC",
                "Completed-order revenue per category.",
            ),
        ),
        (
            "total_revenue",
            "What is the total revenue from completed orders?",
            [r"revenue"],
            lambda q: (
                f"SELECT {_REVENUE} AS total_revenue FROM orders o "
                f"JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status = 'completed'",
                "Total revenue of completed orders.",
            ),
        ),
        (
            "top_products",
            "Show the top-selling products by quantity.",
            [r"(top.selling|best.selling|most (frequently )?(ordered|sold)|most popular)"],
            lambda q: (
                f"SELECT p.product_id, p.product_name, SUM(oi.quantity) AS units_sold "
                f"FROM products p JOIN order_items oi ON oi.product_id = p.product_id "
                f"JOIN orders o ON o.order_id = oi.order_id WHERE o.status = 'completed' "
                f"GROUP BY p.product_id, p.product_name ORDER BY units_sold DESC, p.product_name "
                f"LIMIT {_number(q, 5)}",
                "Products ranked by units on completed orders.",
            ),
        ),
        (
            "avg_order_value",
            "What is the average value of completed orders?",
            [r"average|avg", r"order"],
            lambda q: (
                f"SELECT ROUND(AVG(order_total), 2) AS average_order_value FROM ("
                f"SELECT o.order_id, {_REVENUE} AS order_total FROM orders o "
                f"JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status = 'completed' "
                f"GROUP BY o.order_id) t",
                "Average of per-order totals, completed orders only.",
            ),
        ),
        (
            "count_orders",
            "How many orders are pending?",
            [r"(how many|count|number of)", r"order"],
            lambda q: (
                f"SELECT COUNT(*) AS order_count FROM orders WHERE status = '{_status(q)}'",
                "Number of orders in the given status.",
            ),
        ),
    ]


class DemoGenerator:
    def __init__(self, dialect: str = "mysql"):
        self.dialect = dialect
        self._templates = _templates()

    def examples(self) -> list[str]:
        return [t[1] for t in self._templates]

    def generate(self, question: str) -> GeneratedSQL:
        q = question.lower()
        for _id, _ex, patterns, build in self._templates:
            if all(re.search(p, q) for p in patterns):
                sql, desc = build(q)
                if self.dialect != "mysql":
                    sql = sqlglot.transpile(sql, read="mysql", write=self.dialect)[0]
                return GeneratedSQL(sql=sql, mode="demo", description=desc)
        raise UnsupportedQuestionError(
            "Demo mode only supports a fixed set of questions (see /examples). "
            "Start Ollama to answer free-form questions."
        )


class SQLGenerator:
    """Chooses between the local LLM and the labelled demo fallback."""

    def __init__(
        self, settings: Settings, ollama: OllamaGenerator | None = None, demo: DemoGenerator | None = None
    ):
        self.s = settings
        self.ollama = ollama or OllamaGenerator(settings)
        self.demo = demo or DemoGenerator(settings.dialect)

    def llm_available(self) -> bool:
        return self.s.llm_mode != "demo" and self.ollama.is_available()

    def generate(self, question: str) -> GeneratedSQL:
        if self.s.llm_mode == "demo":
            return self.demo.generate(question)
        try:
            return self.ollama.generate(question)
        except LLMUnavailableError as exc:
            if self.s.llm_mode != "auto":
                raise
            try:
                result = self.demo.generate(question)
            except UnsupportedQuestionError as unsupported:
                raise LLMUnavailableError(
                    f"{exc.message} The demo fallback also cannot answer this question: {unsupported.message}"
                ) from exc
            result.notes.append("Local LLM unavailable: used the deterministic demo fallback (not an LLM).")
            return result
