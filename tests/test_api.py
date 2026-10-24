import httpx
import pytest
from sqlalchemy import create_engine, text

from tests.conftest import make_service


def llm(sql):
    return lambda req: httpx.Response(200, json={"message": {"content": sql}})


def test_health_schema_examples(client_factory):
    c = client_factory()
    h = c.get("/health").json()
    assert h["database"] == "up" and h["llm"] == "disabled" and "password" not in str(h).lower()
    tables = {t["name"]: t for t in c.get("/schema").json()["tables"]}
    assert set(tables) == {"customers", "products", "orders", "order_items"}
    assert "email" not in [col["name"] for col in tables["customers"]["columns"]]
    ex = c.get("/examples").json()["examples"]
    assert len(ex) >= 8


EXAMPLE_CHECKS = {
    "Which products have fewer than ten units in stock?": (
        "SELECT COUNT(*) FROM products WHERE stock_quantity < 10",
        None,
    ),
    "How many orders are pending?": ("SELECT COUNT(*) FROM orders WHERE status='pending'", None),
}


def test_demo_examples_all_execute(client_factory):
    c = client_factory()
    for q in c.get("/examples").json()["examples"]:
        r = c.post("/query", json={"question": q})
        assert r.status_code == 200, (q, r.json())
        body = r.json()
        assert body["status"] == "ok" and body["generation_mode"] == "demo"
        assert body["columns"] and body["execution_time_ms"] is not None and body["explanation"]


def test_results_match_database(client_factory, admin_engine):
    c = client_factory()
    body = c.post("/query", json={"question": "How many orders are pending?"}).json()
    with admin_engine.connect() as conn:
        expected = conn.execute(text("SELECT COUNT(*) FROM orders WHERE status='pending'")).scalar_one()
    assert body["rows"] == [[expected]]

    body = c.post("/query", json={"question": "Show the five customers who spent the most"}).json()
    with admin_engine.connect() as conn:
        top = (
            conn.execute(
                text(
                    "SELECT c.name FROM customers c JOIN orders o ON o.customer_id=c.customer_id "
                    "JOIN order_items oi ON oi.order_id=o.order_id WHERE o.status='completed' "
                    "GROUP BY c.customer_id ORDER BY SUM(oi.quantity*oi.unit_price) DESC LIMIT 5"
                )
            )
            .scalars()
            .all()
        )
    assert [r[1] for r in body["rows"]] == top and len(top) == 5

    body = c.post("/query", json={"question": "Which customers have never placed an order?"}).json()
    assert len(body["rows"]) == 3


@pytest.mark.parametrize(
    "payload", [{}, {"question": ""}, {"question": "ab"}, {"question": "x" * 501}, {"question": 5}]
)
def test_invalid_requests(client_factory, payload):
    assert client_factory().post("/query", json=payload).status_code == 422


def test_oversized_body_rejected(client_factory):
    r = client_factory().post(
        "/query", content=b"{" + b" " * 20000 + b"}", headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


def test_unsupported_demo_question(client_factory):
    r = client_factory().post("/query", json={"question": "what is the meaning of life"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "UNSUPPORTED_QUESTION"


def test_llm_success_labelled_llm(settings, app_engine, client_factory):
    svc = make_service(
        settings, app_engine, llm("SELECT name FROM customers WHERE city='Pune'"), mode="ollama"
    )
    body = client_factory(svc).post("/query", json={"question": "customers in Pune"}).json()
    assert body["generation_mode"] == "llm" and body["status"] == "ok" and len(body["rows"]) == 2


def test_llm_unavailable_ollama_mode_is_503(settings, app_engine, client_factory):
    def boom(req):
        raise httpx.ConnectError("down")

    svc = make_service(settings, app_engine, boom, mode="ollama")
    r = client_factory(svc).post("/query", json={"question": "customers in Pune"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "LLM_UNAVAILABLE"


def test_auto_mode_falls_back_to_labelled_demo(settings, app_engine, client_factory):
    def boom(req):
        raise httpx.ConnectError("down")

    svc = make_service(settings, app_engine, boom, mode="auto")
    c = client_factory(svc)
    body = c.post("/query", json={"question": "How many orders are pending?"}).json()
    assert body["generation_mode"] == "demo" and any("not an LLM" in n for n in body["notes"])
    r = c.post("/query", json={"question": "free form unsupported question here"})
    assert r.status_code == 503 and "demo fallback" in r.json()["error"]["message"]


def test_malformed_model_output(settings, app_engine, client_factory):
    svc = make_service(settings, app_engine, llm("Sure! Here you go: the customers are many"), mode="ollama")
    r = client_factory(svc).post("/query", json={"question": "customers please"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "MODEL_OUTPUT_INVALID"
    svc = make_service(
        settings,
        app_engine,
        lambda req: httpx.Response(200, json={"message": {"content": ""}}),
        mode="ollama",
    )
    assert client_factory(svc).post("/query", json={"question": "customers please"}).status_code == 502


@pytest.mark.parametrize(
    "evil",
    [
        "DROP TABLE customers",
        "DELETE FROM orders",
        "SELECT * FROM customers; DROP TABLE orders",
        "SELECT * FROM sqlite_master",
        "SELECT email FROM customers",
        "UPDATE products SET price=0",
    ],
)
def test_malicious_llm_output_rejected_and_never_executed(
    settings, app_engine, admin_engine, client_factory, evil
):
    svc = make_service(settings, app_engine, llm(evil), mode="ollama")
    calls = []
    original = svc.executor.execute
    svc.executor.execute = lambda v: (calls.append(v), original(v))[1]
    r = client_factory(svc).post("/query", json={"question": "customers please"})
    assert r.status_code == 400 and r.json()["status"] == "rejected"
    assert r.json()["executed_sql"] is None and r.json()["rows"] == []
    assert calls == []  # executor never invoked
    with admin_engine.connect() as c:  # and data is intact
        assert c.execute(text("SELECT COUNT(*) FROM customers")).scalar_one() == 16


def test_executor_refuses_unvalidated_sql(service):
    with pytest.raises(TypeError):
        service.executor.execute("SELECT 1")


def test_row_limit_enforced(settings, app_engine, client_factory):
    s = settings.model_copy(update={"max_rows": 5})
    svc = make_service(s, app_engine, llm("SELECT item_id FROM order_items"), mode="ollama")
    body = client_factory(svc).post("/query", json={"question": "all items please"}).json()
    assert body["row_count"] == 5 and "LIMIT 5" in body["executed_sql"]


def test_database_unavailable_is_safe(settings, client_factory):
    bad = create_engine("sqlite:////nonexistent_dir/x.db")
    svc = make_service(settings, bad)
    r = client_factory(svc).post("/query", json={"question": "How many orders are pending?"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "nonexistent" not in r.text and "Traceback" not in r.text
    assert client_factory(svc).get("/health").json()["database"] == "down"


def test_query_timeout(settings, app_engine, client_factory):
    s = settings.model_copy(update={"query_timeout_seconds": 0.05})
    heavy = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 50000000) "
        "SELECT COUNT(*) FROM n"
    )
    svc = make_service(s, app_engine, llm(heavy), mode="ollama")
    r = client_factory(svc).post("/query", json={"question": "something heavy please"})
    assert r.status_code == 504 and r.json()["error"]["code"] == "QUERY_TIMEOUT"


def test_unhandled_error_is_generic(settings, app_engine, client_factory):
    svc = make_service(settings, app_engine)
    svc.run = lambda q: (_ for _ in ()).throw(RuntimeError("secret /home/x/path password=abc"))
    r = client_factory(svc).post("/query", json={"question": "How many orders are pending?"})
    assert r.status_code == 500 and "secret" not in r.text and "password" not in r.text
