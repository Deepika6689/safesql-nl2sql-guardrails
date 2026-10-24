import json

import httpx
import pytest

from app.config import Settings
from app.services.sql_generator import (
    DemoGenerator,
    LLMOutputError,
    LLMTimeoutError,
    LLMUnavailableError,
    OllamaGenerator,
    UnsupportedQuestionError,
    build_prompt,
    extract_sql,
    select_relevant_tables,
)


def gen(handler, **kw):
    s = Settings(database_url="sqlite://", _env_file=None, **kw)
    return OllamaGenerator(s, client=httpx.Client(transport=httpx.MockTransport(handler)))


def reply(text):
    return lambda req: httpx.Response(200, json={"message": {"content": text}})


def test_extract_sql_variants():
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("  SELECT 2  ") == "SELECT 2"
    with pytest.raises(LLMOutputError):
        extract_sql("   ")
    with pytest.raises(LLMOutputError):
        extract_sql("```sql\n```")


def test_ollama_success_is_labelled_llm():
    r = gen(reply("```sql\nSELECT name FROM customers\n```")).generate("list customers")
    assert (r.mode, r.sql) == ("llm", "SELECT name FROM customers")


def test_ollama_request_contains_only_schema_not_secrets():
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"message": {"content": "SELECT 1"}})

    s = Settings(database_url="sqlite://", mysql_password="hunter2secret", _env_file=None)
    OllamaGenerator(s, client=httpx.Client(transport=httpx.MockTransport(handler))).generate(
        "customers by city"
    )
    assert (
        "hunter2secret" not in seen["body"]
        and "email" not in json.loads(seen["body"])["messages"][0]["content"]
    )


def test_unreachable_missing_model_timeout_and_malformed():
    def boom(req):
        raise httpx.ConnectError("no")

    with pytest.raises(LLMUnavailableError):
        gen(boom).generate("x y z")
    with pytest.raises(LLMUnavailableError) as e:
        gen(lambda r: httpx.Response(404, json={"error": "model not found"})).generate("x y z")
    assert "ollama pull" in e.value.message

    def slow(req):
        raise httpx.ReadTimeout("t")

    with pytest.raises(LLMTimeoutError):
        gen(slow).generate("x y z")
    with pytest.raises(LLMOutputError):
        gen(lambda r: httpx.Response(200, json={"unexpected": 1})).generate("x y z")
    with pytest.raises(LLMOutputError):
        gen(lambda r: httpx.Response(200, text="not json")).generate("x y z")


def test_schema_pruning_and_prompt_excludes_pii():
    assert select_relevant_tables("which products are low on stock") == ["products"]
    t = select_relevant_tables("revenue by product category")
    assert {"products", "orders", "order_items"} <= set(t)
    system, _ = build_prompt("customers by city", "mysql")
    assert "email" not in system and "customers" in system


def test_demo_unsupported_is_explicit():
    with pytest.raises(UnsupportedQuestionError):
        DemoGenerator("sqlite").generate("What is the airspeed of a swallow?")
    r = DemoGenerator("sqlite").generate("How many orders are pending?")
    assert r.mode == "demo" and "pending" in r.sql
