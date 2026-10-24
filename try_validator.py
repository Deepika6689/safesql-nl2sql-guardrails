from app.services.sql_validator import SQLValidator, SQLValidationError

v = SQLValidator("sqlite")
tests = [
    "SELECT name FROM customers",
    "DROP TABLE customers",
    "SELECT * FROM customers; DELETE FROM orders",
    "SELECT email FROM customers",
    "SELECT * FROM sqlite_master",
    "SELECT SLEEP(5)",
]
for sql in tests:
    try:
        result = v.validate(sql)
        print("ALLOWED :", sql, "->", result.sql)
    except SQLValidationError as e:
        print("BLOCKED :", sql, "->", e.message)