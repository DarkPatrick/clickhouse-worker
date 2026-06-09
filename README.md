# clickhouse-worker

`clickhouse-worker` is a small Python helper library for working with
ClickHouse through [`clickhouse-connect`](https://clickhouse.com/docs/integrations/python).
It wraps the common operations used by application code:

- create a ClickHouse client from environment variables or an explicit config;
- execute read-only SQL and return a `pandas.DataFrame`;
- execute modifying SQL commands;
- insert `pandas.DataFrame` data into ClickHouse;
- generate simple ClickHouse column definitions from pandas dtypes;
- normalize SQL and convert large integers to JSON-safe values.

The library intentionally stays thin. It does not implement migrations, query
builders, connection lifecycle management, retries, or schema inference beyond a
small dtype helper.

## Requirements

- Python `>=3.13`
- `clickhouse_connect>=0.15.1`
- `pandas>=3.0.2`

## Installation

From this repository:

```bash
pip install .
```

For editable local development:

```bash
pip install -e .
```

## Configuration

By default, `create_client()` reads connection settings from environment
variables with the `CLICKHOUSE_` prefix.

| Environment variable | Default | Description |
| --- | --- | --- |
| `CLICKHOUSE_HOST` | `None` | ClickHouse host. Required for real connections. |
| `CLICKHOUSE_PORT` | `8443` | ClickHouse HTTP/HTTPS port. |
| `CLICKHOUSE_USERNAME` | `None` | Username. |
| `CLICKHOUSE_PASSWORD` | `""` | Password. |
| `CLICKHOUSE_SECURE` | `1` | Uses TLS unless set to `0`, `false`, or `no`. |
| `CLICKHOUSE_VERIFY` | `0` | Verifies TLS certificates only when set to `1`, `true`, or `yes`. |

Example:

```bash
export CLICKHOUSE_HOST="example.clickhouse.cloud"
export CLICKHOUSE_PORT="8443"
export CLICKHOUSE_USERNAME="default"
export CLICKHOUSE_PASSWORD="secret"
export CLICKHOUSE_SECURE="1"
export CLICKHOUSE_VERIFY="1"
```

You can also pass configuration explicitly:

```python
from clickhouse_worker import ClickHouseConfig, create_client

config = ClickHouseConfig(
    host="example.clickhouse.cloud",
    port=8443,
    username="default",
    password="secret",
    secure=True,
    verify=True,
)

client = create_client(config)
try:
    print(client.ping())
finally:
    client.close()
```

## Quick Start

### Run a SELECT query

```python
from clickhouse_worker import execute_sql

df = execute_sql("""
    SELECT
        database,
        name,
        engine
    FROM system.tables
    WHERE database = 'default'
""")

print(df.head())
```

`execute_sql()` returns a `pandas.DataFrame`.

### Limit returned rows

```python
from clickhouse_worker import execute_sql

df = execute_sql("SELECT * FROM events ORDER BY created_at DESC", max_rows=100)
```

When `max_rows` is provided, the library wraps the query as:

```sql
select * from (<original query>) limit <max_rows>
```

### Run DDL or modifying SQL

```python
from clickhouse_worker import execute_sql_modify

execute_sql_modify("""
    CREATE TABLE IF NOT EXISTS default.events
    (
        id UInt64,
        name String,
        created_at DateTime
    )
    ENGINE = MergeTree
    ORDER BY id
""")
```

Use `execute_sql_modify()` for commands executed through
`clickhouse-connect`'s `client.command()`, for example `CREATE`, `ALTER`,
`DROP`, `TRUNCATE`, or lightweight commands.

### Insert a DataFrame

```python
import pandas as pd
from clickhouse_worker import insert_dataframe

df = pd.DataFrame(
    [
        {"id": 1, "name": "signup", "created_at": "2026-06-09 10:00:00"},
        {"id": 2, "name": "purchase", "created_at": "2026-06-09 10:05:00"},
    ]
)

insert_dataframe("default.events", df)
```

`insert_dataframe()` delegates to `clickhouse-connect`'s `client.insert_df()`.
The DataFrame columns must match the target table structure expected by
ClickHouse.

### Insert in chunks

```python
from clickhouse_worker import insert_df_by_chunks

insert_df_by_chunks("default.events", df, chunk_size=10_000)
```

You can also transform the DataFrame once before chunking:

```python
def prepare_events(df):
    prepared = df.copy()
    prepared["created_at"] = pd.to_datetime(prepared["created_at"])
    return prepared

insert_df_by_chunks(
    "default.events",
    df,
    chunk_size=10_000,
    prepare=prepare_events,
)
```

Important: `insert_df_by_chunks()` uses the default `insert_dataframe()`
internally and does not currently accept a custom `client_factory`.

## Public API

Import all public helpers from `clickhouse_worker`.

```python
from clickhouse_worker import (
    ClickHouseConfig,
    ClickHouseConnectionError,
    ClickHouseQueryError,
    clickhouse_string_literal,
    create_client,
    execute_sql,
    execute_sql_modify,
    insert_dataframe,
    insert_df_by_chunks,
    json_safe_cell,
    pandas_to_clickhouse_types,
    sanitize_sql,
)
```

### `ClickHouseConfig`

```python
ClickHouseConfig(
    host: str | None,
    port: int = 8443,
    username: str | None = None,
    password: str = "",
    secure: bool = True,
    verify: bool = False,
)
```

Frozen dataclass with connection settings.

Use `ClickHouseConfig.from_env(prefix="CLICKHOUSE_")` to load values from
environment variables. A different prefix can be used for multiple ClickHouse
connections:

```python
analytics_config = ClickHouseConfig.from_env("ANALYTICS_CLICKHOUSE_")
```

### `create_client(config=None)`

Creates a `clickhouse_connect` client and immediately calls `client.ping()`.

Raises:

- `ClickHouseConnectionError` when the client cannot be created or ping fails.

Returns:

- a raw `clickhouse-connect` client. The caller owns it and should close it.

### `sanitize_sql(sql, allow_multiple_statements=False)`

Normalizes SQL before execution:

- treats `None` and empty strings as invalid;
- strips surrounding whitespace;
- strips leading/trailing semicolons;
- dedents multiline strings;
- rejects multiple statements by default if a semicolon remains inside the SQL.

Raises:

- `ClickHouseQueryError("SQL is empty.")`
- `ClickHouseQueryError("Only one SQL statement is allowed (remove extra ';').")`

Use `allow_multiple_statements=True` only when you are deliberately preparing
SQL for a caller that supports multiple statements. The execution helpers in
this package call `sanitize_sql()` with the default single-statement behavior.

### `execute_sql(sql, max_rows=None, client_factory=create_client)`

Executes a query with `client.query()` and returns a `pandas.DataFrame`.

Parameters:

- `sql`: SQL query string.
- `max_rows`: optional integer row limit.
- `client_factory`: zero-argument callable that returns a ClickHouse client.
  This is useful for tests or custom connection setup.

Behavior:

- sanitizes SQL;
- creates a client;
- executes the query;
- converts `result.result_rows` and `result.column_names` into a DataFrame;
- always closes the client in `finally`.

Raises:

- `ClickHouseQueryError` for ClickHouse errors, invalid responses, and
  unexpected exceptions.

### `execute_sql_modify(sql, client_factory=create_client)`

Executes SQL with `client.command()` and returns `None`.

Use this for DDL or commands that should not return tabular data.

Behavior and error handling are the same as `execute_sql()`: SQL is sanitized,
the client is closed in `finally`, and errors are wrapped in
`ClickHouseQueryError`.

### `insert_dataframe(table_name, df, client_factory=create_client)`

Inserts a full DataFrame into `table_name` with `client.insert_df()`.

Parameters:

- `table_name`: ClickHouse table name, for example `"default.events"`.
- `df`: `pandas.DataFrame`.
- `client_factory`: zero-argument callable that returns a ClickHouse client.

The function closes the client after the insert.

### `insert_df_by_chunks(table_name, df, chunk_size=1000, prepare=None)`

Splits a DataFrame into chunks and inserts each chunk with `insert_dataframe()`.

Parameters:

- `table_name`: ClickHouse table name.
- `df`: source `pandas.DataFrame`.
- `chunk_size`: maximum number of rows per insert.
- `prepare`: optional callable that receives the original DataFrame and returns
  a prepared DataFrame before chunking starts.

The function logs chunk boundaries through the module logger.

### `pandas_to_clickhouse_types(df)`

Returns a comma-separated string with simple ClickHouse column definitions.

Current dtype mapping:

| pandas dtype | ClickHouse type |
| --- | --- |
| `int64` | `Int64` |
| `float64` | `Float64` |
| `object` | `String` |
| `datetime64[ns]` | `DateTime` |
| anything else | `String` |

Example:

```python
columns_sql = pandas_to_clickhouse_types(df)

create_sql = f"""
CREATE TABLE default.events
(
{columns_sql}
)
ENGINE = MergeTree
ORDER BY tuple()
"""
```

Column names are wrapped in backticks. This helper is intentionally simple:
review generated types before using the result in production DDL.

### `clickhouse_string_literal(value)`

Returns a single-quoted ClickHouse string literal with backslashes and single
quotes escaped.

```python
clickhouse_string_literal("Bob's event")
# "'Bob\\'s event'"
```

Prefer query parameters or `clickhouse-connect` structured APIs when available.
Use this helper only for small, controlled SQL construction cases.

### `json_safe_cell(value)`

Converts values for JSON responses that may be read by JavaScript clients.

Behavior:

- `bool` values stay booleans;
- integers within JavaScript's safe integer range stay integers;
- integers larger than `2**53 - 1` or smaller than `-(2**53 - 1)` become
  strings;
- all other values are returned unchanged.

This prevents loss of precision for large ClickHouse integer IDs in JavaScript.

### Exceptions

`ClickHouseConnectionError`

- raised by `create_client()` when connecting or pinging fails.

`ClickHouseQueryError`

- raised by SQL execution and insert helpers for user-facing query/response
  failures.

## Testing With a Fake Client

The main operation helpers accept `client_factory`, so tests can avoid a real
ClickHouse connection.

```python
from clickhouse_worker import execute_sql


class FakeResult:
    column_names = ["id", "name"]
    result_rows = [(1, "alpha")]
    first_row = result_rows[0]


class FakeClient:
    def query(self, sql):
        self.sql = sql
        return FakeResult()

    def close(self):
        pass


def fake_client_factory():
    return FakeClient()


df = execute_sql("SELECT 1 AS id, 'alpha' AS name", client_factory=fake_client_factory)
assert df.to_dict("records") == [{"id": 1, "name": "alpha"}]
```

For modifying queries, implement `command(sql)` on the fake client. For inserts,
implement `insert_df(table_name, df)`.

## Agent Notes

This section is written for AI coding agents and future maintainers.

- The package has a deliberately small public API. `clickhouse_worker/__init__.py`
  re-exports the intended symbols.
- The implementation is in `clickhouse_worker/client.py`.
- `_get_client()` and `_sanitize_sql()` are backward-compatible aliases for
  older application code. They are not exported from `__init__.py`.
- Every helper that creates a client should close it in `finally`.
- `clickhouse_connect.get_client()` uses HTTP connection pooling internally;
  short-lived clients around operations are acceptable for this library's
  intended use.
- Keep exceptions user-facing. The current pattern wraps low-level errors in
  `ClickHouseConnectionError` or `ClickHouseQueryError`.
- `sanitize_sql()` is not a SQL injection prevention system. It only performs
  basic normalization and rejects accidental multiple statements.
- `pandas_to_clickhouse_types()` is intentionally conservative and incomplete.
  Extend the mapping carefully when new pandas dtypes are needed.
- `insert_df_by_chunks()` currently has less dependency-injection support than
  the other helpers because it calls `insert_dataframe()` directly.

## Logging

The module logger is named after `clickhouse_worker.client`.

Logged events include:

- failed connection attempts;
- a sample cell type from the first returned row in `execute_sql()`;
- empty query result notices;
- chunk insert ranges in `insert_df_by_chunks()`.

Configure logging in your application if you want to see these messages:

```python
import logging

logging.basicConfig(level=logging.INFO)
```

## Development Notes

Recommended local checks:

```bash
python -m compileall clickhouse_worker
python - <<'PY'
from clickhouse_worker import ClickHouseConfig, sanitize_sql

assert ClickHouseConfig.from_env()
assert sanitize_sql("  SELECT 1;  ") == "SELECT 1"
print("ok")
PY
```

There is no test suite in the repository at the moment.

## License

No license file is currently included in this repository. Add one before
publishing or redistributing the package.
