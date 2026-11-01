"""Role permissions come only from role_permissions rows: table- and column-level rules, the
schema each role's AI prompts see, the SQL guardrail in every clause, and fail-closed loading."""
import json

import pytest
from sqlalchemy import create_engine, text

from app.access import AccessController
from app.guardrails import GuardrailError, tables_for_role, validate_sql
from app.pipeline import DENIED_ERROR, Pipeline
from tests.fakes import FakeDB, ScriptedLLM, plan_json
from tests.support import BILLING_PERMISSIONS, BILLING_TABLES

# The live billing schema (as discovered from MySQL), used by the fake database below.
SCHEMA = {
    "products": ["product_id", "name", "description", "active"],
    "plans": ["plan_id", "product_id", "nickname", "billing_interval", "amount_cents", "currency", "active"],
    "customers": ["customer_id", "name", "email", "country", "created_at"],
    "payment_methods": ["payment_method_id", "customer_id", "type", "card_brand", "card_last4",
                        "exp_month", "exp_year", "is_default", "created_at"],
    "subscriptions": ["subscription_id", "customer_id", "plan_id", "status", "start_date",
                      "current_period_start", "current_period_end", "cancel_at_period_end", "canceled_at"],
    "invoices": ["invoice_id", "subscription_id", "customer_id", "amount_due_cents", "amount_paid_cents",
                 "currency", "status", "period_start", "period_end", "due_date", "created_at"],
    "charges": ["charge_id", "invoice_id", "payment_method_id", "customer_id", "amount_cents",
                "currency", "status", "failure_code", "created_at"],
    "refunds": ["refund_id", "charge_id", "amount_cents", "reason", "created_at"],
}
DENIED = "not authorized"


class SchemaDB(FakeDB):
    def describe(self, tables):
        return "\n".join(f"{t}(" + ", ".join(f"{c} VARCHAR" for c in SCHEMA[t]) + ")" for t in tables)

    def table_columns(self, tables):
        return {t: [(c, "VARCHAR") for c in SCHEMA[t]] for t in tables}


def access(rows=None):
    return AccessController(BILLING_PERMISSIONS if rows is None else rows)


def check(sql, role, acc=None):
    acc = acc or access()
    return validate_sql(sql, "mysql", tables_for_role(BILLING_TABLES, role, acc), 1000,
                        role=role, access=acc)


def with_rule(role, kind, name, allowed, rows=None):
    """BILLING_PERMISSIONS with one row changed or added, as an UPDATE/INSERT would."""
    rows = [dict(r) for r in (rows or BILLING_PERMISSIONS)
            if (r["role"], r["resource_type"], r["resource_name"]) != (role, kind, name)]
    return rows + [{"role": role, "resource_type": kind, "resource_name": name, "allowed": allowed}]


# ------------------------------------------------------------------ partially restricted tables
@pytest.mark.parametrize("role", ["Developer", "Analyst"])
@pytest.mark.parametrize("sql", [
    "SELECT customer_id, name, country, created_at FROM customers",
    "SELECT c.name, c.country FROM customers AS c WHERE c.country = 'IN' ORDER BY c.name",
    "SELECT country, COUNT(*) AS customers FROM customers GROUP BY country HAVING COUNT(*) > 1",
    "SELECT c.name, SUM(i.amount_due_cents) AS due FROM customers c "
    "JOIN invoices i ON i.customer_id = c.customer_id GROUP BY c.name",
    "WITH recent AS (SELECT customer_id, country FROM customers) SELECT country FROM recent",
    "SELECT i.* FROM invoices i JOIN customers c ON c.customer_id = i.customer_id",
    "SELECT * FROM plans",
    "SELECT country, COUNT(*) AS n FROM customers GROUP BY country HAVING n > 1 ORDER BY n DESC",
])
def test_permitted_columns_of_a_partially_restricted_table_are_allowed(sql, role):
    assert check(sql, role).tables


@pytest.mark.parametrize("role", ["Developer", "Analyst"])
@pytest.mark.parametrize("sql", [
    "SELECT * FROM customers",
    "SELECT c.* FROM customers c",
    "SELECT * FROM invoices i JOIN customers c ON c.customer_id = i.customer_id",
])
def test_wildcard_over_a_restricted_column_asks_for_explicit_columns(sql, role):
    with pytest.raises(GuardrailError) as exc:
        check(sql, role)
    # Retryable (the model lists permitted columns), and the restricted name is not revealed.
    assert DENIED not in str(exc.value).lower() and "email" not in str(exc.value).lower()


# ------------------------------------------------------------------ denied columns in any clause
@pytest.mark.parametrize("role", ["Developer", "Analyst", "Finance Analyst"])
@pytest.mark.parametrize("sql", [
    "SELECT email FROM customers",
    "SELECT name FROM customers WHERE email LIKE '%@gmail.com'",
    "SELECT c.name FROM customers c JOIN invoices i ON i.customer_id = c.customer_id AND c.email <> ''",
    "SELECT email, COUNT(*) AS n FROM customers GROUP BY email",
    "SELECT name FROM customers ORDER BY email",
    "SELECT country FROM customers GROUP BY country HAVING MAX(email) > 'a'",
    "SELECT COUNT(DISTINCT email) AS n FROM customers",
    "SELECT CONCAT(name, email) AS label FROM customers",
    "SELECT CASE WHEN email IS NULL THEN 'none' ELSE 'some' END AS has_mail FROM customers",
    "SELECT LENGTH(c.email) AS n FROM customers c",
    "SELECT name FROM customers WHERE customer_id IN (SELECT customer_id FROM customers WHERE email = 'x')",
    "SELECT (SELECT MAX(email) FROM customers) AS m FROM invoices",
    "WITH e AS (SELECT email AS contact FROM customers) SELECT contact FROM e",
    "SELECT x.contact FROM (SELECT c.email AS contact FROM customers c) AS x",
    "SELECT c.name FROM customers c WHERE EXISTS "
    "(SELECT 1 FROM invoices i WHERE i.customer_id = c.customer_id AND c.email LIKE 'a%')",
    "SELECT name FROM customers c WHERE EXISTS (SELECT 1 FROM invoices i WHERE email LIKE 'a%')",
    "SELECT name FROM customers UNION SELECT email FROM customers",
    "SELECT i.invoice_id FROM invoices i JOIN customers c USING (customer_id, email)",
])
def test_denied_column_is_refused_in_every_clause(sql, role):
    with pytest.raises(GuardrailError) as exc:
        check(sql, role)
    assert DENIED in str(exc.value).lower()


@pytest.mark.parametrize("role", ["Developer", "Analyst"])
@pytest.mark.parametrize("sql", [
    "SELECT card_brand FROM payment_methods",
    "SELECT COUNT(*) AS n FROM payment_methods",
    "SELECT c.charge_id FROM charges c JOIN payment_methods p ON p.payment_method_id = c.payment_method_id",
    "SELECT charge_id FROM charges WHERE payment_method_id IN (SELECT payment_method_id FROM payment_methods)",
    "WITH p AS (SELECT * FROM payment_methods) SELECT COUNT(*) AS n FROM p",
])
def test_denied_table_is_refused_directly_and_indirectly(sql, role):
    with pytest.raises(GuardrailError) as exc:
        check(sql, role)
    assert DENIED in str(exc.value).lower()          # a denial, not a "table not available" retry


def test_natural_join_and_unknown_qualifiers_are_never_executed():
    for sql in ("SELECT name FROM customers NATURAL JOIN invoices",
                "SELECT z.email FROM customers c"):
        with pytest.raises(GuardrailError):
            check(sql, "Developer")


# ------------------------------------------------------------------ roles and permission data
@pytest.mark.parametrize("role", [None, "", "admin", "Auditor", "ceo2"])
def test_unknown_roles_are_denied(role):
    acc = access()
    assert not acc.can_access_table(role, "charges")
    with pytest.raises(GuardrailError):
        check("SELECT charge_id FROM charges", role, acc)


def test_missing_permission_data_fails_closed_for_every_role_including_ceo():
    empty = AccessController([])
    for role in ("CEO", "Developer", "Analyst"):
        assert not empty.can_access_table(role, "charges")
        with pytest.raises(GuardrailError):
            check("SELECT charge_id FROM charges", role, empty)


def test_invalid_rows_grant_nothing_and_an_unreadable_flag_denies():
    acc = AccessController([
        {"role": "Developer", "resource_type": "table", "resource_name": "customers", "allowed": 1},
        {"role": "Developer", "resource_type": "column", "resource_name": "customers.email", "allowed": "maybe"},
        {"role": "Developer", "resource_type": "view", "resource_name": "invoices", "allowed": 1},
        {"role": "Developer", "resource_type": "table", "resource_name": "refunds", "allowed": None},
        {"role": "Developer", "resource_type": "table"},                       # missing fields
        {"role": "Nobody", "resource_type": "table", "resource_name": "charges", "allowed": 1},
    ])
    assert acc.can_access_column("Developer", "customers", "name")
    assert not acc.can_access_column("Developer", "customers", "email")
    assert not acc.can_access_table("Developer", "invoices")
    assert not acc.can_access_table("Developer", "refunds")
    assert not acc.can_access_table("Nobody", "charges")


def test_ceo_access_follows_the_database_rows():
    acc = access()
    assert check("SELECT c.email, p.card_brand FROM customers c "
                 "JOIN payment_methods p ON p.customer_id = c.customer_id", "CEO", acc)
    assert check("SELECT * FROM customers", "CEO", acc)

    no_email = access(with_rule("CEO", "column", "customers.email", 0))
    with pytest.raises(GuardrailError, match=DENIED):
        check("SELECT email FROM customers", "CEO", no_email)
    with pytest.raises(GuardrailError):
        check("SELECT * FROM customers", "CEO", no_email)
    assert check("SELECT name FROM customers", "CEO", no_email)

    no_cards = access(with_rule("CEO", "table", "payment_methods", 0))
    with pytest.raises(GuardrailError, match=DENIED):
        check("SELECT card_brand FROM payment_methods", "CEO", no_cards)
    without_row = [r for r in BILLING_PERMISSIONS if (r["role"], r["resource_name"]) != ("CEO", "refunds")]
    assert not access(without_row).can_access_table("CEO", "refunds")


def test_table_wide_column_rule_and_explicit_column_override():
    rows = with_rule("Analyst", "column", "invoices.*", 0)
    rows = with_rule("Analyst", "column", "invoices.status", 1, rows)
    acc = access(rows)
    assert acc.permitted_columns("Analyst", "invoices", SCHEMA["invoices"]) == ["status"]
    with pytest.raises(GuardrailError):
        check("SELECT * FROM invoices", "Analyst", acc)


# ------------------------------------------------------------------ live table changes
@pytest.fixture
def permissions_db():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE role_permissions (permission_id INTEGER PRIMARY KEY, "
                          "role TEXT, resource_type TEXT, resource_name TEXT, allowed INTEGER)"))
        for r in BILLING_PERMISSIONS:
            conn.execute(text("INSERT INTO role_permissions (role, resource_type, resource_name, allowed) "
                              "VALUES (:role, :resource_type, :resource_name, :allowed)"),
                         {**r, "allowed": int(r["allowed"])})
    return engine


def test_changing_role_permissions_changes_authorization_without_code_changes(permissions_db):
    before = AccessController.from_engine(permissions_db)
    with pytest.raises(GuardrailError, match=DENIED):
        check("SELECT email FROM customers", "Developer", before)
    assert check("SELECT country FROM customers", "Developer", before)

    with permissions_db.begin() as conn:
        conn.execute(text("UPDATE role_permissions SET allowed = 1 WHERE role = 'Developer' "
                          "AND resource_name = 'customers.email'"))
        conn.execute(text("INSERT INTO role_permissions (role, resource_type, resource_name, allowed) "
                          "VALUES ('Developer', 'column', 'customers.country', 0)"))
        conn.execute(text("UPDATE role_permissions SET allowed = 0 WHERE role = 'Analyst' "
                          "AND resource_name = 'refunds'"))
    after = AccessController.from_engine(permissions_db)
    assert check("SELECT email FROM customers", "Developer", after)
    with pytest.raises(GuardrailError, match=DENIED):
        check("SELECT country FROM customers", "Developer", after)
    with pytest.raises(GuardrailError, match=DENIED):
        check("SELECT refund_id FROM refunds", "Analyst", after)

    pipe = Pipeline(ScriptedLLM([]), SchemaDB(), cfg(), base_dir=".")
    assert "customers(customer_id VARCHAR, name VARCHAR, email VARCHAR, created_at VARCHAR)" \
        in pipe.schema_for("Developer", after)
    assert "refunds(" not in pipe.schema_for("Analyst", after)


def test_permission_table_that_cannot_be_read_raises_instead_of_allowing():
    with pytest.raises(Exception):
        AccessController.from_engine(create_engine("sqlite://"))


# ------------------------------------------------------------------ the AI sees the permitted schema
def cfg():
    return json.loads(json.dumps({"db": {"allowed_tables": BILLING_TABLES, "max_rows": 1000},
                                  "context": {"files": []}, "pipeline": {"max_retries": 2}}))


@pytest.mark.parametrize("role", ["Developer", "Analyst"])
def test_planner_and_sql_prompts_list_only_permitted_tables_and_columns(role):
    pipe = Pipeline(ScriptedLLM([]), SchemaDB(), cfg(), base_dir=".")
    acc = access()
    schema, sql_prompt = pipe.schema_for(role, acc), pipe.sql_system(role, acc)
    for text_ in (schema, sql_prompt):
        assert "customers(customer_id VARCHAR, name VARCHAR, country VARCHAR, created_at VARCHAR)" in text_
        assert "email" not in text_ and "payment_methods" not in text_ and "card_brand" not in text_
    # The planner alone is told what exists but is restricted, so it never calls it missing data.
    planner = pipe.planner_system("state", role, acc)
    assert schema in planner and "Restricted for this user" in planner
    assert "payment_methods(" in planner and "customers.email" in planner
    ceo = pipe.planner_system("state", "CEO", acc)
    assert "email VARCHAR" in ceo and "payment_methods(" in ceo and "Restricted for this user" not in ceo
    assert "(no tables are available" in pipe.schema_for("Auditor", acc)


def test_show_the_customer_table_returns_permitted_columns_for_a_developer():
    star = "```sql\nSELECT * FROM customers\n```"
    explicit = "```sql\nSELECT customer_id, name, country, created_at FROM customers\n```"
    llm, db = ScriptedLLM([star, explicit]), SchemaDB(columns=("customer_id", "name", "country", "created_at"))
    pipe = Pipeline(llm, db, cfg(), base_dir=".")
    result = pipe.generate_sql("Show the customer table", role="Developer", access=access())
    assert result.status == "ok" and result.attempts == 2
    assert "email" not in result.sql.lower() and "*" not in result.sql
    assert db.ran == [result.sql]                                    # SELECT * never reached the DB
    assert "SELECT * is not allowed" in llm.calls[1][1][-1]["content"]


def test_requesting_a_restricted_column_is_denied_without_running_sql():
    sneaky = "```sql\nSELECT name, email FROM customers\n```"
    llm, db = ScriptedLLM([sneaky, sneaky, sneaky]), SchemaDB()
    result = Pipeline(llm, db, cfg(), base_dir=".").generate_sql(
        "customer names and emails", role="Analyst", access=access())
    assert result.status == "failed" and result.error == DENIED_ERROR
    assert db.ran == [] and len(llm.calls) == 1                      # denied, not retried around


def test_pre_llm_check_uses_database_rules_and_discovered_columns():
    pipe = Pipeline(ScriptedLLM([]), SchemaDB(), cfg(), base_dir=".")     # no configured wording
    acc = access()
    assert pipe.denied_reference("Developer", acc, "customer emails by country") == "customers.email"
    assert pipe.denied_reference("Analyst", acc, "count by card_last4") == "payment_methods"
    assert pipe.denied_reference("Developer", acc, "show the customer table") is None
    assert pipe.denied_reference("CEO", acc, "customer emails by country") is None
    # A bare column name shared with a readable column elsewhere is not treated as a request.
    rows = with_rule("Developer", "column", "subscriptions.status", 0)
    assert pipe.denied_reference("Developer", access(rows), "invoice status by month") is None
    assert pipe.denied_reference("Developer", access(rows), "subscriptions status") == "subscriptions.status"


def test_ask_denies_before_the_planner_and_passes_the_role_schema_to_it():
    llm = ScriptedLLM([plan_json("chat", reply="Hi")])
    pipe = Pipeline(llm, SchemaDB(), cfg(), base_dir=".")
    assert pipe.ask("list customer emails", role="Developer", access=access()).error == DENIED_ERROR
    assert llm.calls == []
    pipe.ask("what can you do?", role="Developer", access=access())
    assert pipe.schema_for("Developer", access()) in llm.calls[0][0]
    assert "email VARCHAR" not in llm.calls[0][0]
