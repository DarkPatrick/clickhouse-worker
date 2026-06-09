from __future__ import annotations

from dataclasses import dataclass
import logging
import numbers
import os
import textwrap
from typing import Any, Callable, Optional

import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError
import pandas as pd


MAX_SAFE_JS_INT = 2**53 - 1
SCALAR_SUBQUERY_EMPTY_RESULT_MESSAGE = "Scalar subquery returned empty result"
SCALAR_SUBQUERY_EMPTY_RESULT_MAX_RETRIES = 5

logger = logging.getLogger(__name__)


class ClickHouseQueryError(Exception):
    """User-facing ClickHouse error with safe message."""


class ClickHouseConnectionError(RuntimeError):
    """Raised when the application cannot connect to ClickHouse."""


@dataclass(frozen=True)
class ClickHouseConfig:
    host: Optional[str]
    port: int = 8443
    username: Optional[str] = None
    password: str = ""
    secure: bool = True
    verify: bool = False

    @classmethod
    def from_env(cls, prefix: str = "CLICKHOUSE_") -> "ClickHouseConfig":
        return cls(
            host=os.environ.get(f"{prefix}HOST"),
            port=int(os.environ.get(f"{prefix}PORT", "8443")),
            username=os.environ.get(f"{prefix}USERNAME"),
            password=os.environ.get(f"{prefix}PASSWORD", ""),
            secure=os.environ.get(f"{prefix}SECURE", "1").lower() not in {"0", "false", "no"},
            verify=os.environ.get(f"{prefix}VERIFY", "0").lower() in {"1", "true", "yes"},
        )


def create_client(config: Optional[ClickHouseConfig] = None):
    """
    Create a ClickHouse client lazily.

    clickhouse_connect internally pools HTTP connections, so creating short-lived
    clients around operations is fine for this app and for a small reusable helper
    library.
    """
    config = config or ClickHouseConfig.from_env()
    try:
        client = clickhouse_connect.get_client(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            secure=config.secure,
            verify=config.verify,
        )
        client.ping()
    except Exception as exc:
        logger.error("Failed to connect to ClickHouse: %s", exc)
        raise ClickHouseConnectionError(f"Failed to connect to ClickHouse: {exc}") from exc

    return client


def _get_client():
    """Backward-compatible alias for older app code."""
    return create_client()


def sanitize_sql(sql: str, *, allow_multiple_statements: bool = False) -> str:
    sql = (sql or "").strip().strip(";")
    if not sql:
        raise ClickHouseQueryError("SQL is empty.")
    if not allow_multiple_statements and ";" in sql:
        raise ClickHouseQueryError("Only one SQL statement is allowed (remove extra ';').")
    return textwrap.dedent(sql).strip()


def _sanitize_sql(sql: str) -> str:
    """Backward-compatible alias for older app code."""
    return sanitize_sql(sql)


def clickhouse_string_literal(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def json_safe_cell(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        int_value = int(value)
        if int_value > MAX_SAFE_JS_INT or int_value < -MAX_SAFE_JS_INT:
            return str(int_value)
        return int_value
    return value


def _is_scalar_subquery_empty_result(exc: Exception) -> bool:
    return SCALAR_SUBQUERY_EMPTY_RESULT_MESSAGE in str(exc)


def _query_to_dataframe(client: Any, sql: str) -> pd.DataFrame:
    result = client.query(sql)
    columns = list(result.column_names or [])
    if result.first_row:
        for index, cell in enumerate(result.first_row):
            logger.info("cell info: col=%s value=%s type=%s", columns[index], cell, type(cell))
    else:
        logger.info("ClickHouse returned no rows")

    return pd.DataFrame(result.result_rows, columns=result.column_names)


def execute_sql_modify(sql: str, *, client_factory: Callable[[], Any] = create_client) -> None:
    sql = sanitize_sql(sql)
    client = client_factory()
    try:
        client.command(sql)
    except ClickHouseError as exc:
        raise ClickHouseQueryError(str(exc)) from exc
    except ValueError as exc:
        raise ClickHouseQueryError(f"Invalid response: {exc}") from exc
    except Exception as exc:
        raise ClickHouseQueryError(f"Unexpected error: {exc}") from exc
    finally:
        client.close()


def execute_sql(
    sql: str,
    *,
    max_rows: Optional[int] = None,
    client_factory: Callable[[], Any] = create_client,
) -> pd.DataFrame:
    sql = sanitize_sql(sql)
    if max_rows is not None:
        sql = f"select * from ({sql}) limit {int(max_rows)}"

    for attempt in range(SCALAR_SUBQUERY_EMPTY_RESULT_MAX_RETRIES + 1):
        client = client_factory()
        try:
            return _query_to_dataframe(client, sql)
        except ClickHouseError as exc:
            if (
                _is_scalar_subquery_empty_result(exc)
                and attempt < SCALAR_SUBQUERY_EMPTY_RESULT_MAX_RETRIES
            ):
                logger.warning(
                    "Retrying ClickHouse query after scalar subquery empty result "
                    "(attempt %s/%s)",
                    attempt + 1,
                    SCALAR_SUBQUERY_EMPTY_RESULT_MAX_RETRIES,
                )
                continue
            raise ClickHouseQueryError(str(exc)) from exc
        except ValueError as exc:
            raise ClickHouseQueryError(f"Invalid response: {exc}") from exc
        except Exception as exc:
            raise ClickHouseQueryError(f"Unexpected error: {exc}") from exc
        finally:
            client.close()

    raise ClickHouseQueryError(SCALAR_SUBQUERY_EMPTY_RESULT_MESSAGE)


def insert_dataframe(
    table_name: str,
    df: pd.DataFrame,
    *,
    client_factory: Callable[[], Any] = create_client,
) -> None:
    client = client_factory()
    try:
        client.insert_df(table_name, df)
    except ClickHouseError as exc:
        raise ClickHouseQueryError(str(exc)) from exc
    except ValueError as exc:
        raise ClickHouseQueryError(f"Invalid response: {exc}") from exc
    except Exception as exc:
        raise ClickHouseQueryError(f"Unexpected error: {exc}") from exc
    finally:
        client.close()


def insert_df_by_chunks(
    table_name: str,
    df: pd.DataFrame,
    *,
    chunk_size: int = 1000,
    prepare: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None,
) -> None:
    prepared_df = prepare(df) if prepare else df
    total = len(prepared_df)

    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        chunk = prepared_df.iloc[start:end].copy()
        logger.info("Insert rows %s - %s / %s into %s", start, end, total, table_name)
        insert_dataframe(table_name, chunk)


def pandas_to_clickhouse_types(df: pd.DataFrame) -> str:
    mapping = {
        "int64": "Int64",
        "float64": "Float64",
        "object": "String",
        "datetime64[ns]": "DateTime",
    }
    cols = []
    for col, dtype in df.dtypes.items():
        clickhouse_type = mapping.get(str(dtype), "String")
        cols.append(f"`{col}` {clickhouse_type}")
    return ",\n".join(cols)
