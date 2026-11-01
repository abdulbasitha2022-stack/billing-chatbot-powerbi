# NL → SQL POC

Ask a business question in plain English → an LLM writes SQL → guardrails check it → it runs
read-only on your database → you get the result. The LLM vendor and the database are both
swappable from `config.yaml`.

## Setup

1. **Database with the sample data** (from `generate_sample_data.py`, in the parent step):
   ```
   docker compose up -d
   python generate_sample_data.py --load
   ```
   This also creates the read-only `poc_readonly` user this project connects as.

2. **Install**
   ```
   pip install -r requirements.txt
   ```

3. **API key** (never put it in a file that goes into git)
   - macOS / Linux: `export ANTHROPIC_API_KEY=sk-ant-...`
   - Windows PowerShell: `$env:ANTHROPIC_API_KEY="sk-ant-..."`

4. **Run**
   ```
   python run.py "How many households are enrolled in AutoBill?"
   python run.py --file demo_questions.txt        # 8 questions, one per dictionary trap
   python run.py                                  # interactive
   python run.py --csv out.csv "..."              # save the rows
   python run.py --show-prompt                    # see exactly what the model is sent
   ```

## How it works

```
question
  → app/llm.py         LLMProvider.complete()       (Claude today, OpenAI stub, add more)
  → app/prompt.py      system prompt = rules + LIVE schema + data dictionary + env notes
  → app/pipeline.py    parse reply: CLARIFY question | ASSUMPTION lines + ```sql block
  → app/guardrails.py  sqlglot: one SELECT, whitelisted tables, no writes, LIMIT enforced
  → app/db.py          DBProvider.execute_readonly()  (read-only txn + statement timeout)
  → on rejection or DB error: the exact error goes back to the model, up to 2 retries
  → logs/requests.jsonl  who asked, SQL, tokens, cached tokens, latency, status
```

## Swapping the AI agent or the database

Edit `config.yaml`, no code changes:

| Want | Change |
|---|---|
| OpenAI instead of Claude | `llm.provider: openai` (+ `pip install openai`, `OPENAI_API_KEY`) |
| Another Claude model | `llm.claude.model` |
| Another database | `db.type: sqlalchemy` and `db.url` (e.g. a BigQuery SQLAlchemy URL) |
| A new client | copy `config.yaml`, swap `context.files` and `context.extra_rules`, run with `--config` |
| A new vendor | subclass `LLMProvider` in `app/llm.py`, register it in `PROVIDERS` |

Prompt quality is **not** portable between vendors. Expect a tuning pass per provider.

## Three independent safety layers

1. **Guardrails** (`app/guardrails.py`): the SQL is parsed, not trusted.
2. **Read-only transaction + timeout** (`PostgresProvider`).
3. **`poc_readonly` role**: the database itself has no write permission.

## Tests

```
pytest -q                                   # 45 tests, no API key or database needed
TEST_DATABASE_URL=postgresql+psycopg2://poc_readonly:poc_readonly@localhost:5432/membership pytest -q
```
The second form also runs the real-Postgres integration test.

## What has and hasn't been verified

Verified: guardrails (dangerous SQL blocked, normal analytics SQL passes), the retry/self-correction
loop, logging, live schema introspection, read-only enforcement on a real Postgres, and the real
CLI up to the network call.

**Not verified yet:** a live Claude response (no API key was available when this was built), the
OpenAI provider, and any non-Postgres database. The model writes the SQL, so run
`demo_questions.txt` and check each answer against SQL you wrote by hand before trusting the
numbers. Turning that into an automated accuracy check is the natural next step.

## Next steps toward the deck

- Expose the validated query as a **view** in a separate schema for Power BI (needs a second,
  write-scoped role for that schema only; keep `poc_readonly` read-only).
- Accuracy eval: question → hand-written "gold" SQL → compare results.
- A small UI (Streamlit / FastAPI) in front of `Pipeline.ask()`.
- Per-user access control before the query runs.
