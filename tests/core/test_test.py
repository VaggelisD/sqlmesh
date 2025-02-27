from __future__ import annotations

import datetime
import typing as t
from pathlib import Path

import pandas as pd
import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import exp

from sqlmesh.cli.example_project import init_example_project
from sqlmesh.core import constants as c
from sqlmesh.core.config import Config, DuckDBConnectionConfig, ModelDefaultsConfig
from sqlmesh.core.context import Context
from sqlmesh.core.dialect import parse
from sqlmesh.core.model import PythonModel, SqlModel, load_sql_based_model, model
from sqlmesh.core.test.definition import PythonModelTest, SqlModelTest
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.yaml import load as load_yaml

if t.TYPE_CHECKING:
    from unittest import TestResult

pytestmark = pytest.mark.slow

SUSHI_FOO_META = "MODEL (name sushi.foo, kind FULL)"


@t.overload
def _create_test(
    body: t.Dict[str, t.Any], test_name: str, model: SqlModel, context: Context
) -> SqlModelTest: ...


@t.overload
def _create_test(
    body: t.Dict[str, t.Any], test_name: str, model: PythonModel, context: Context
) -> PythonModelTest: ...


def _create_test(body, test_name, model, context):
    test_type = SqlModelTest if isinstance(model, SqlModel) else PythonModelTest
    return test_type(
        body=body[test_name],
        test_name=test_name,
        model=model,
        models=context._models,
        engine_adapter=context._test_engine_adapter,
        dialect=context.config.dialect,
        path=None,
        default_catalog=context.default_catalog,
    )


def _create_model(
    query: str,
    meta: str = SUSHI_FOO_META,
    dialect: t.Optional[str] = None,
    default_catalog: t.Optional[str] = None,
) -> SqlModel:
    parsed_definition = parse(f"{meta};{query}", default_dialect=dialect)
    return t.cast(
        SqlModel,
        load_sql_based_model(parsed_definition, dialect=dialect, default_catalog=default_catalog),
    )


def _check_successful_or_raise(
    result: t.Optional[TestResult], expected_msg: t.Optional[str] = None
) -> None:
    assert result is not None
    if not result.wasSuccessful():
        error_or_failure_traceback = (result.errors or result.failures)[0][1]
        if expected_msg:
            assert expected_msg in error_or_failure_traceback
        else:
            raise AssertionError(error_or_failure_traceback)


@pytest.fixture
def full_model_without_ctes(request) -> SqlModel:
    return _create_model(
        "SELECT id, value, ds FROM raw",
        dialect=getattr(request, "param", None),
        default_catalog="memory",
    )


@pytest.fixture
def full_model_with_single_cte(request) -> SqlModel:
    return _create_model(
        "WITH source AS (SELECT id FROM raw) SELECT id FROM source",
        dialect=getattr(request, "param", None),
        default_catalog="memory",
    )


@pytest.fixture
def full_model_with_two_ctes(request) -> SqlModel:
    return _create_model(
        """
        WITH source AS (
            SELECT id FROM raw
        ),
        renamed AS (
            SELECT id AS fid FROM source
        )
        SELECT fid FROM renamed;
        """,
        dialect=getattr(request, "param", None),
        default_catalog="memory",
    )


def test_ctes(sushi_context: Context, full_model_with_two_ctes: SqlModel) -> None:
    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_with_two_ctes))
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - id: 1
  outputs:
    ctes:
      source:
        - id: 1
      renamed:
        - fid: 1
    query:
      - fid: 1
  vars:
    start: 2022-01-01
    end: 2022-01-01
        """
    )
    test = _create_test(body, "test_foo", model, sushi_context)

    random_id = "jzngz56a"
    test._test_id = random_id
    _check_successful_or_raise(test.run())

    assert len(test._fixture_table_cache) == len(sushi_context.models) + 1
    for table in test._fixture_table_cache.values():
        assert table.name.endswith(f"__fixture__{random_id}")


def test_ctes_only(sushi_context: Context, full_model_with_two_ctes: SqlModel) -> None:
    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_with_two_ctes))
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - id: 1
  outputs:
    ctes:
      source:
        - id: 1
      renamed:
        - fid: 1
  vars:
    start: 2022-01-01
    end: 2022-01-01
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


def test_query_only(sushi_context: Context, full_model_with_two_ctes: SqlModel) -> None:
    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_with_two_ctes))
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - id: 1
  outputs:
    query:
      - fid: 1
  vars:
    start: 2022-01-01
    end: 2022-01-01
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


def test_with_rows(sushi_context: Context, full_model_with_single_cte: SqlModel) -> None:
    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_with_single_cte))
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      rows:
        - id: 1
  outputs:
    ctes:
      source:
        rows:
          - id: 1
    query:
      rows:
        - id: 1
  vars:
    start: 2022-01-01
    end: 2022-01-01
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


def test_without_rows(sushi_context: Context, full_model_with_single_cte: SqlModel) -> None:
    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_with_single_cte))
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - id: 1
  outputs:
    ctes:
      source:
        - id: 1
    query:
      - id: 1
  vars:
    start: 2022-01-01
    end: 2022-01-01
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


def test_column_order(sushi_context: Context, full_model_without_ctes: SqlModel) -> None:
    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_without_ctes))
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - id: 1
        value: 2
        ds: 3
  outputs:
    query:
      - id: 1
        ds: 3
        value: 2
  vars:
    start: 2022-01-01
    end: 2022-01-01
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


def test_row_order(sushi_context: Context, full_model_without_ctes: SqlModel) -> None:
    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_without_ctes))

    # input and output rows are in different orders
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - id: 1
        value: 2
        ds: 3
      - id: 2
        value: 3
        ds: 4
  outputs:
    query:
      - id: 2
        value: 3
        ds: 4
      - id: 1
        value: 2
        ds: 3
  vars:
    start: 2022-01-01
    end: 2022-01-01
        """
    )

    # model query without ORDER BY should pass unit test
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)

    # model query with ORDER BY should fail unit test
    full_model_without_ctes_dict = full_model_without_ctes.dict()
    full_model_without_ctes_dict["query"] = full_model_without_ctes.query.order_by("id")  # type: ignore
    full_model_without_ctes_orderby = SqlModel(**full_model_without_ctes_dict)

    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_without_ctes_orderby))
    result = _create_test(body, "test_foo", model, sushi_context).run()

    expected_failure_msg = (
        "AssertionError: Data mismatch (exp: expected, act: actual)\n\n"
        "   id     value      ds    \n"
        "  exp act   exp act exp act\n"
        "0   2   1     3   2   4   3\n"
        "1   1   2     2   3   3   4\n"
    )
    _check_successful_or_raise(result, expected_msg=expected_failure_msg)


def test_partial_data(sushi_context: Context) -> None:
    model = _create_model(
        "WITH source AS (SELECT id, name FROM sushi.waiter_names) SELECT id, name, 'nan' as str FROM source",
        default_catalog=sushi_context.default_catalog,
    )
    model = t.cast(SqlModel, sushi_context.upsert_model(model))

    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    sushi.waiter_names:
      - id: 1
      - id: 2
        name: null
      - id: 3
        name: 'bob'
  outputs:
    ctes:
      source:
        - id: 1
        - id: 2
          name: null
        - id: 3
          name: 'bob'
    query:
      - id: 1
        str: nan
      - id: 2
        str: nan
      - id: 3
        name: 'bob'
        str: nan
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


def test_partial_output_columns() -> None:
    result = _create_test(
        body=load_yaml(
            """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - a: 1
        b: 2
        c: 3
        d: 4
      - a: 5
        b: 6
        c: 7
  outputs:
    partial: true  # Applies to all outputs
    ctes:
      t:
        rows:
          - c: 3
          - c: 7
    query:
      rows:
        - a: 1
          b: 2
        - a: 5
          b: 6
            """
        ),
        test_name="test_foo",
        model=_create_model("WITH t AS (SELECT a, b, c, d FROM raw) SELECT a, b, c, d FROM t"),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    ).run()

    _check_successful_or_raise(result)

    result = _create_test(
        body=load_yaml(
            """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - a: 1
        b: 2
        c: 3
        d: 4
      - a: 5
        b: 6
        c: 7
  outputs:
    ctes:
      t:
        partial: true
        rows:
          - c: 3
          - c: 7
    query:
      rows:
        - a: 1
          b: 2
          c: 3
          d: 4
        - a: 5
          b: 6
          c: 7
            """
        ),
        test_name="test_foo",
        model=_create_model("WITH t AS (SELECT a, b, c, d FROM raw) SELECT a, b, c, d FROM t"),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    ).run()


def test_partial_data_column_order(sushi_context: Context) -> None:
    model = _create_model(
        "SELECT id, name, price, event_date FROM sushi.items",
        default_catalog=sushi_context.default_catalog,
    )
    model = t.cast(SqlModel, sushi_context.upsert_model(model))

    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    sushi.items:
      - id: 1234
        event_date: 2020-01-01
      - id: 9876
        name: hello
        event_date: 2020-01-02
  outputs:
    query:
      - id: 1234
        event_date: 2020-01-01
      - id: 9876
        name: hello
        event_date: 2020-01-02
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


def test_partial_data_missing_schemas(sushi_context: Context) -> None:
    model = _create_model("SELECT * FROM unknown")
    model = t.cast(SqlModel, sushi_context.upsert_model(model))

    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    unknown:
      - a: 1
        b: bla
      - b: baz
  outputs:
    query:
      - a: 1
        b: bla
      - b: baz
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)

    model = _create_model(
        "SELECT *, DATE_TRUNC('month', date)::DATE AS month, NULL::DATE AS null_date, FROM unknown"
    )
    model = t.cast(SqlModel, sushi_context.upsert_model(model))

    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    unknown:
      - id: 1234
        date: 2023-01-12
      - id: 9876
        date: 2023-02-10
  outputs:
    query:
      - id: 1234
        date: 2023-01-12
        month: 2023-01-01
        null_date:
      - id: 9876
        date: 2023-02-10
        month: 2023-02-01
        null_date:
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


def test_missing_column_failure(sushi_context: Context, full_model_without_ctes: SqlModel) -> None:
    model = t.cast(SqlModel, sushi_context.upsert_model(full_model_without_ctes))
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  description: sushi.foo's output has a missing column (fails intentionally)
  inputs:
    raw:
      - id: 1
        value: 2
        ds: 3
  outputs:
    query:
      - id: 1
        value: null
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()

    expected_failure_msg = (
        "AssertionError: Data mismatch (exp: expected, act: actual)\n\n"
        "  value      ds    \n"
        "    exp act exp act\n"
        "0   NaN   2 NaN   3\n"
    )
    _check_successful_or_raise(result, expected_msg=expected_failure_msg)


def test_row_difference_failure() -> None:
    result = _create_test(
        body=load_yaml(
            """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - value: 1
  outputs:
    query:
      - value: 1
      - value: 2
            """
        ),
        test_name="test_foo",
        model=_create_model("SELECT value FROM raw"),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    ).run()

    expected_error_msg = (
        "AssertionError: Data mismatch (rows are different)\n\n"
        "Missing rows:\n\n"
        "   value\n"
        "0      2\n"
    )
    _check_successful_or_raise(result, expected_msg=expected_error_msg)

    result = _create_test(
        body=load_yaml(
            """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - value: 1
  outputs:
    query:
      - value: 1
            """
        ),
        test_name="test_foo",
        model=_create_model("SELECT value FROM raw UNION ALL SELECT value + 1 AS value FROM raw"),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    ).run()

    expected_error_msg = (
        "AssertionError: Data mismatch (rows are different)\n\n"
        "Unexpected rows:\n\n"
        "   value\n"
        "0      2\n"
    )
    _check_successful_or_raise(result, expected_msg=expected_error_msg)

    result = _create_test(
        body=load_yaml(
            """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - value: 1
  outputs:
    query:
      - value: 1
      - value: 3
      - value: 4
            """
        ),
        test_name="test_foo",
        model=_create_model("SELECT value FROM raw UNION ALL SELECT value + 1 AS value FROM raw"),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    ).run()

    expected_error_msg = (
        "AssertionError: Data mismatch (rows are different)\n\n"
        "Missing rows:\n\n"
        "   value\n"
        "0      3\n"
        "1      4\n\n"
        "Unexpected rows:\n\n"
        "   value\n"
        "0      2\n"
    )
    _check_successful_or_raise(result, expected_msg=expected_error_msg)


def test_unknown_column_error() -> None:
    result = _create_test(
        body=load_yaml(
            """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - id: 1
        value: 2
  outputs:
    query:
      - foo: 1
            """
        ),
        test_name="test_foo",
        model=_create_model("SELECT id, value FROM raw"),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    ).run()

    expected_error_msg = (
        "sqlmesh.core.test.definition.TestError: Detected unknown column(s)\n\n"
        "Expected column(s): id, value\n"
        "Unknown column(s): foo\n"
    )
    _check_successful_or_raise(result, expected_msg=expected_error_msg)


def test_empty_rows(sushi_context: Context) -> None:
    model = _create_model(
        "SELECT id FROM sushi.items", default_catalog=sushi_context.default_catalog
    )
    model = t.cast(SqlModel, sushi_context.upsert_model(model))

    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    sushi.items: []
  outputs:
    query: []
        """
    )
    result = _create_test(body, "test_foo", model, sushi_context).run()
    _check_successful_or_raise(result)


@pytest.mark.parametrize("full_model_without_ctes", ["snowflake"], indirect=True)
def test_normalization(full_model_without_ctes: SqlModel) -> None:
    body = load_yaml(
        """
test_foo:
  model: sushi.foo
  inputs:
    raw:
      - id: 1
  outputs:
    ctes:
      source:
        rows:
          - id: 1
      renamed:
        - fid: 1
    query:
      - fid: 1
  vars:
    start: 2022-01-01
    end: 2022-01-01
        """
    )

    context = Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="snowflake")))
    normalized_body = _create_test(body, "test_foo", full_model_without_ctes, context).body

    expected_body = {
        "model": '"MEMORY"."SUSHI"."FOO"',
        "inputs": {'"RAW"': {"rows": [{"ID": 1}]}},
        "outputs": {
            "ctes": {'"SOURCE"': {"rows": [{"ID": 1}]}, '"RENAMED"': {"rows": [{"FID": 1}]}},
            "query": {"rows": [{"FID": 1}]},
        },
        "vars": {"start": datetime.date(2022, 1, 1), "end": datetime.date(2022, 1, 1)},
    }

    assert expected_body == normalized_body


def test_test_generation(tmp_path: Path) -> None:
    init_example_project(tmp_path, dialect="duckdb")

    config = Config(
        default_connection=DuckDBConnectionConfig(),
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    context = Context(paths=tmp_path, config=config)

    query = context.get_model("sqlmesh_example.full_model").render_query()
    assert isinstance(query, exp.Query)

    context.upsert_model(
        "sqlmesh_example.full_model",
        query=exp.select(*query.named_selects).from_("cte").with_("cte", as_=query),
    )

    context.plan(auto_apply=True)

    input_queries = {
        "sqlmesh_example.incremental_model": "SELECT * FROM sqlmesh_example.incremental_model LIMIT 3"
    }

    with pytest.raises(ConfigError) as ex:
        context.create_test("sqlmesh_example.full_model", input_queries=input_queries)

    assert (
        "tests/test_full_model.yaml' already exists, "
        "make sure to set --overwrite if it can be safely overwritten."
    ) in str(ex.value)

    test = load_yaml(context.path / c.TESTS / "test_full_model.yaml")

    assert len(test) == 1
    assert "test_example_full_model" in test
    assert "vars" not in test["test_example_full_model"]
    assert "ctes" not in test["test_example_full_model"]["outputs"]

    context.create_test(
        "sqlmesh_example.full_model",
        input_queries=input_queries,
        overwrite=True,
        variables={"start": "2020-01-01", "end": "2024-01-01"},
        include_ctes=True,
    )

    test = load_yaml(context.path / c.TESTS / "test_full_model.yaml")

    assert len(test) == 1
    assert "test_full_model" in test
    assert "vars" in test["test_full_model"]
    assert test["test_full_model"]["vars"] == {"start": "2020-01-01", "end": "2024-01-01"}
    assert "ctes" in test["test_full_model"]["outputs"]
    assert "cte" in test["test_full_model"]["outputs"]["ctes"]

    result = context.test()
    _check_successful_or_raise(result)

    context.create_test(
        "sqlmesh_example.full_model",
        input_queries=input_queries,
        name="new_name",
        path="foo/bar",
    )

    test = load_yaml(context.path / c.TESTS / "foo/bar.yaml")
    assert len(test) == 1
    assert "new_name" in test
    assert "ctes" not in test["new_name"]["outputs"]


def test_source_func() -> None:
    result = _create_test(
        body=load_yaml(
            """
test_foo:
  model: xyz
  outputs:
    query:
      - month: 2023-01-01
      - month: 2023-02-01
      - month: 2023-03-01
            """
        ),
        test_name="test_foo",
        model=_create_model(
            """
            SELECT range::DATE AS month
            FROM RANGE(DATE '2023-01-01', DATE '2023-04-01', INTERVAL 1 MONTH) AS r
            """
        ),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    ).run()

    _check_successful_or_raise(result)


def test_nested_data_types() -> None:
    raw = _create_model(
        "SELECT array::INT[], struct::STRUCT(x INT[], y VARCHAR, z INT, w STRUCT(a INT)) FROM sushi.unknown",
        meta="MODEL (name sushi.raw, kind FULL)",
        default_catalog="memory",
    )
    context = Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb")))
    context.upsert_model(raw)

    result = _create_test(
        body=load_yaml(
            """
test_foo:
  model: sushi.foo
  inputs:
    sushi.raw:
      - array: [1, 2, 3]
        struct: {'x': [1, 2, 3], 'y': 'foo', 'z': 1, 'w': {'a': 5}}
      - array:
        - 2
        - 3
      - array: [0, 4, 1]
  outputs:
    query:
      - array: [0, 4, 1]
      - array: [1, 2, 3]
        struct: {'x': [1, 2, 3], 'y': 'foo', 'z': 1, 'w': {'a': 5}}
      - array: [2, 3]
            """
        ),
        test_name="test_foo",
        model=_create_model("SELECT array, struct FROM sushi.raw", default_catalog="memory"),
        context=context,
    ).run()

    _check_successful_or_raise(result)


def test_freeze_time(mocker: MockerFixture) -> None:
    test = _create_test(
        body=load_yaml(
            """
test_foo:
  model: xyz
  outputs:
    query:
      - cur_date: 2023-01-01
        cur_time: 12:05:03
        cur_timestamp: "2023-01-01 12:05:03"
  vars:
    execution_time: "2023-01-01 12:05:03+00:00"
            """
        ),
        test_name="test_foo",
        model=_create_model(
            "SELECT CURRENT_DATE AS cur_date, CURRENT_TIME AS cur_time, CURRENT_TIMESTAMP AS cur_timestamp"
        ),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    )

    spy_execute = mocker.spy(test.engine_adapter, "_execute")
    _check_successful_or_raise(test.run())

    spy_execute.assert_called_with(
        "SELECT "
        """CAST('2023-01-01 12:05:03+00:00' AS DATE) AS "cur_date", """
        """CAST('2023-01-01 12:05:03+00:00' AS TIME) AS "cur_time", """
        '''CAST('2023-01-01 12:05:03+00:00' AS TIMESTAMP) AS "cur_timestamp"''',
    )

    @model("py_model", columns={"ts1": "timestamptz", "ts2": "timestamptz"})
    def execute(context, start, end, execution_time, **kwargs):
        datetime_now = datetime.datetime.now()

        context.engine_adapter.execute(exp.select("CURRENT_TIMESTAMP"))
        current_timestamp = context.engine_adapter.cursor.fetchone()[0]

        return pd.DataFrame([{"ts1": datetime_now, "ts2": current_timestamp}])

    test = _create_test(
        body=load_yaml(
            """
test_py_model:
  model: py_model
  outputs:
    query:
      - ts1: "2023-01-01 10:05:03"
        ts2: "2023-01-01 10:05:03"
  vars:
    execution_time: "2023-01-01 12:05:03+02:00"
            """
        ),
        test_name="test_py_model",
        model=model.get_registry()["py_model"].model(module_path=Path("."), path=Path(".")),
        context=Context(config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))),
    )

    _check_successful_or_raise(test.run())


def test_successes(sushi_context: Context) -> None:
    results = sushi_context.test()
    successful_tests = [success.test_name for success in results.successes]  # type: ignore
    assert len(successful_tests) == 2
    assert "test_order_items" in successful_tests
    assert "test_customer_revenue_by_day" in successful_tests


def test_test_generation_with_data_structures(tmp_path: Path) -> None:
    def create_test(query: str):
        context.create_test(
            "sqlmesh_example.foo",
            input_queries={"sqlmesh_example.bar": query},
            overwrite=True,
        )
        return load_yaml(context.path / c.TESTS / "test_foo.yaml")

    init_example_project(tmp_path, dialect="duckdb")

    config = Config(
        default_connection=DuckDBConnectionConfig(),
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    foo_sql_file = tmp_path / "models" / "foo.sql"
    foo_sql_file.write_text(
        "MODEL (name sqlmesh_example.foo); SELECT col FROM sqlmesh_example.bar;"
    )
    bar_sql_file = tmp_path / "models" / "bar.sql"
    bar_sql_file.write_text("MODEL (name sqlmesh_example.bar); SELECT col FROM external_table;")

    context = Context(paths=tmp_path, config=config)

    # Array of strings
    test = create_test("SELECT ['value1', 'value2'] AS col")
    expected_value: t.Any = [{"col": ["value1", "value2"]}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Array of arrays
    test = create_test("SELECT [['value1'], ['value2', 'value3']] AS col")
    expected_value = [{"col": [["value1"], ["value2", "value3"]]}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Array of maps
    test = create_test("SELECT [MAP {'key': 'value1'}, MAP {'key': 'value2'}] AS col")
    expected_value = [{"col": [{"key": "value1"}, {"key": "value2"}]}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Array of structs
    test = create_test("SELECT [{'key': 'value1'}, {'key': 'value2'}] AS col")
    expected_value = [{"col": [{"key": "value1"}, {"key": "value2"}]}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Map of strings
    test = create_test("SELECT MAP {'key1': 'value1', 'key2': 'value2'} AS col")
    expected_value = [{"col": {"key1": "value1", "key2": "value2"}}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Struct of strings
    test = create_test("SELECT {'key1': 'value1', 'key2': 'value2'} AS col")
    expected_value = [{"col": {"key1": "value1", "key2": "value2"}}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Map of arrays
    test = create_test("SELECT MAP {'key1': ['value1'], 'key2': ['value2']} AS col")
    expected_value = [{"col": {"key1": ["value1"], "key2": ["value2"]}}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Struct of arrays
    test = create_test("SELECT {'key1': ['value1'], 'key2': ['value2']} AS col")
    expected_value = [{"col": {"key1": ["value1"], "key2": ["value2"]}}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Map of maps
    test = create_test(
        "SELECT MAP {'key1': MAP {'subkey1': 'value1'}, 'key2': MAP {'subkey2': 'value2'}} AS col"
    )
    expected_value = [{"col": {"key1": {"subkey1": "value1"}, "key2": {"subkey2": "value2"}}}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Map of structs
    test = create_test(
        "SELECT MAP {'key1': {'subkey': 'value1'}, 'key2': {'subkey': 'value2'}} AS col"
    )
    expected_value = [{"col": {"key1": {"subkey": "value1"}, "key2": {"subkey": "value2"}}}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Struct of structs
    test = create_test(
        "SELECT {'key1': {'subkey1': 'value1'}, 'key2': {'subkey2': 'value2'}} AS col"
    )
    expected_value = [{"col": {"key1": {"subkey1": "value1"}, "key2": {"subkey2": "value2"}}}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}

    # Struct of maps
    test = create_test(
        "SELECT {'key1': MAP {'subkey1': 'value1'}, 'key2': MAP {'subkey2': 'value2'}} AS col"
    )
    expected_value = [{"col": {"key1": {"subkey1": "value1"}, "key2": {"subkey2": "value2"}}}]
    assert test["test_foo"]["inputs"] == {"sqlmesh_example.bar": expected_value}
    assert test["test_foo"]["outputs"] == {"query": expected_value}


def test_test_generation_with_timestamp(tmp_path: Path) -> None:
    init_example_project(tmp_path, dialect="duckdb")

    config = Config(
        default_connection=DuckDBConnectionConfig(),
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    foo_sql_file = tmp_path / "models" / "foo.sql"
    foo_sql_file.write_text(
        "MODEL (name sqlmesh_example.foo); SELECT ts_col FROM sqlmesh_example.bar;"
    )
    bar_sql_file = tmp_path / "models" / "bar.sql"
    bar_sql_file.write_text("MODEL (name sqlmesh_example.bar); SELECT ts_col FROM external_table;")

    context = Context(paths=tmp_path, config=config)

    input_queries = {
        "sqlmesh_example.bar": "SELECT TIMESTAMP '2024-09-20 11:30:00.123456789' AS ts_col"
    }

    context.create_test(
        "sqlmesh_example.foo",
        input_queries=input_queries,
        overwrite=True,
    )

    test = load_yaml(context.path / c.TESTS / "test_foo.yaml")

    assert len(test) == 1
    assert "test_foo" in test
    assert test["test_foo"]["inputs"] == {
        "sqlmesh_example.bar": [{"ts_col": datetime.datetime(2024, 9, 20, 11, 30, 0, 123456)}]
    }
    assert test["test_foo"]["outputs"] == {
        "query": [{"ts_col": datetime.datetime(2024, 9, 20, 11, 30, 0, 123456)}]
    }
