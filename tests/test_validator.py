import pytest

from app.security.permissions import ALLOWED_COLUMNS
from app.services.sql_validator import SQLValidationError


def ok(validator, sql):
    return validator.validate(sql)


def rejected(validator, sql):
    with pytest.raises(SQLValidationError) as e:
        validator.validate(sql)
    return e.value.code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",
        "select name from customers where city = 'Pune' order by name",
        "SELECT c.name, COUNT(o.order_id) AS n FROM customers c LEFT JOIN orders o ON o.customer_id=c.customer_id GROUP BY c.name HAVING n > 1 ORDER BY n DESC",
        "SELECT category, AVG(price) FROM products GROUP BY category",
        "WITH big AS (SELECT order_id FROM order_items WHERE quantity > 2) SELECT COUNT(*) FROM big",
        "SELECT name FROM customers WHERE customer_id IN (SELECT customer_id FROM orders WHERE status='pending')",
        "SELECT city FROM customers UNION SELECT category FROM products",
        "SELECT name FROM customers WHERE name LIKE '%a%';",
        "SELECT `name` FROM `customers`",
        "SELECT COUNT(*) FROM orders",
    ],
)
def test_valid_queries_accepted(validator, sql):
    r = ok(validator, sql)
    assert r.sql.upper().startswith(("SELECT", "WITH"))
    assert "LIMIT" in r.sql.upper()


@pytest.mark.parametrize(
    "sql,code",
    [
        ("INSERT INTO customers (name) VALUES ('x')", "STATEMENT_NOT_ALLOWED"),
        ("UPDATE customers SET city='x'", "STATEMENT_NOT_ALLOWED"),
        ("DELETE FROM customers", "STATEMENT_NOT_ALLOWED"),
        ("DROP TABLE customers", "STATEMENT_NOT_ALLOWED"),
        ("ALTER TABLE customers ADD COLUMN x INT", "STATEMENT_NOT_ALLOWED"),
        ("CREATE TABLE t (a INT)", "STATEMENT_NOT_ALLOWED"),
        ("TRUNCATE TABLE customers", "STATEMENT_NOT_ALLOWED"),
        ("GRANT ALL ON *.* TO 'x'", None),
        ("REVOKE ALL ON *.* FROM 'x'", None),
        ("SELECT 1; DROP TABLE customers", "MULTIPLE_STATEMENTS"),
        ("SELECT 1;;SELECT 2", "MULTIPLE_STATEMENTS"),
        ("SELEC name FRM customers", None),
        ("SELECT", None),
        ("", "EMPTY_SQL"),
        ("   ", "EMPTY_SQL"),
    ],
)
def test_prohibited_statements_rejected(validator, sql, code):
    got = rejected(validator, sql)
    if code:
        assert got == code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM mysql.user",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM main.customers",
        "SELECT * FROM customers c JOIN secrets s ON s.id=c.customer_id",
        "SELECT * FROM (SELECT * FROM mysql.user) t",
        "WITH x AS (SELECT * FROM information_schema.columns) SELECT * FROM x",
        "SELECT name FROM customers WHERE customer_id IN (SELECT id FROM users)",
    ],
)
def test_unauthorized_tables_rejected(validator, sql):
    assert rejected(validator, sql) == "UNAUTHORIZED_TABLE"


def test_unknown_and_forbidden_columns_rejected(validator):
    assert rejected(validator, "SELECT password FROM customers") == "UNKNOWN_COLUMN"
    assert rejected(validator, "SELECT email FROM customers") == "UNKNOWN_COLUMN"  # PII not allowlisted
    assert rejected(validator, "SELECT c.email FROM customers c") == "UNKNOWN_COLUMN"
    assert rejected(validator, "SELECT name FROM customers WHERE email LIKE '%@x%'") == "UNKNOWN_COLUMN"


def test_star_expands_only_to_approved_columns(validator):
    sql = ok(validator, "SELECT * FROM customers").sql.lower()
    assert "email" not in sql
    assert all(c in sql for c in ALLOWED_COLUMNS["customers"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers /*!50000 UNION SELECT 1 */",
        "SELECT /*!50000 SLEEP(5) */ 1",
        "SELECT SLEEP(5)",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT BENCHMARK(1000000, MD5('a'))",
        "SELECT @@version",
        "SELECT VERSION()",
        "SELECT USER()",
        "SELECT name FROM customers INTO OUTFILE '/tmp/x'",
        "SELECT name FROM customers FOR UPDATE",
        "SELECT randomblob(100000000)",
        "SELECT 1 INTO @x",
    ],
)
def test_dangerous_constructs_rejected(validator, sql):
    rejected(validator, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SEL/**/ECT 1; DR/**/OP TABLE customers",
        "DR/**/OP TABLE customers",
        "SELECT 1 /* ; */ ; DROP TABLE customers",
        "WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d",
        "SELECT * FROM customers WHERE 1=1; -- harmless\nDELETE FROM orders",
        "select 'a'; select 'b'",
    ],
)
def test_nested_or_obfuscated_rejected(validator, sql):
    rejected(validator, sql)


def test_semicolon_inside_string_is_not_a_second_statement(validator):
    r = ok(validator, "SELECT name FROM customers WHERE city = 'a; DROP TABLE customers'")
    assert "DROP TABLE customers'" in r.sql  # still just a string literal


def test_comments_are_stripped_from_executed_sql(validator):
    r = ok(validator, "SELECT name /* hi */ FROM customers -- trailing")
    assert "/*" not in r.sql and "--" not in r.sql


def test_row_limit_added_lowered_and_kept(validator):
    assert ok(validator, "SELECT name FROM customers").limit == 100
    assert ok(validator, "SELECT name FROM customers LIMIT 100000").limit == 100
    assert ok(validator, "SELECT name FROM customers LIMIT 5").limit == 5
    assert " 5" in ok(validator, "SELECT name FROM customers LIMIT 5").sql


def test_too_long_rejected(validator):
    assert (
        rejected(validator, "SELECT name FROM customers WHERE city = '" + "a" * 3000 + "'") == "SQL_TOO_LONG"
    )


def test_mysql_dialect_validation():
    from app.services.sql_validator import SQLValidator

    v = SQLValidator("mysql", 100)
    assert (
        "DATE_FORMAT"
        in v.validate("SELECT DATE_FORMAT(order_date,'%Y-%m') AS m FROM orders GROUP BY m").sql.upper()
    )
    with pytest.raises(SQLValidationError):
        v.validate("SELECT * FROM mysql.user")
    with pytest.raises(SQLValidationError):
        v.validate("SELECT SLEEP(10)")
