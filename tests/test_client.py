from __future__ import annotations

import sys
import types
import unittest


clickhouse_connect = types.ModuleType("clickhouse_connect")
clickhouse_connect.get_client = lambda **kwargs: None
clickhouse_driver = types.ModuleType("clickhouse_connect.driver")
clickhouse_exceptions = types.ModuleType("clickhouse_connect.driver.exceptions")


class ClickHouseError(Exception):
    pass


clickhouse_exceptions.ClickHouseError = ClickHouseError
sys.modules.setdefault("clickhouse_connect", clickhouse_connect)
sys.modules.setdefault("clickhouse_connect.driver", clickhouse_driver)
sys.modules.setdefault("clickhouse_connect.driver.exceptions", clickhouse_exceptions)

from clickhouse_worker.client import execute_sql


class FakeEmptyResult:
    column_names = ["id"]
    result_rows = []

    @property
    def first_row(self):
        raise IndexError("list index out of range")


class FakeClient:
    closed = False

    def query(self, sql):
        self.sql = sql
        return FakeEmptyResult()

    def close(self):
        self.closed = True


class ExecuteSqlTests(unittest.TestCase):
    def test_empty_result_does_not_read_first_row(self):
        client = FakeClient()

        df = execute_sql("SELECT id FROM empty_table", client_factory=lambda: client)

        self.assertEqual(df.columns.tolist(), ["id"])
        self.assertEqual(df.to_dict("records"), [])
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
