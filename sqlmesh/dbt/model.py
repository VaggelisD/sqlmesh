from __future__ import annotations

import logging
import typing as t

from sqlglot import exp
from sqlglot.helper import ensure_list

from sqlmesh.core import dialect as d
from sqlmesh.core.config.base import UpdateStrategy
from sqlmesh.core.model import (
    EmbeddedKind,
    FullKind,
    IncrementalByTimeRangeKind,
    IncrementalByUniqueKeyKind,
    IncrementalUnmanagedKind,
    Model,
    ModelKind,
    SCDType2ByColumnKind,
    ViewKind,
    create_sql_model,
)
from sqlmesh.core.model.kind import SCDType2ByTimeKind
from sqlmesh.dbt.basemodel import BaseModelConfig, Materialization, SnapshotStrategy
from sqlmesh.dbt.common import SqlStr, extract_jinja_config, sql_str_validator
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import field_validator

if t.TYPE_CHECKING:
    from sqlmesh.dbt.context import DbtContext


logger = logging.getLogger(__name__)

INCREMENTAL_BY_TIME_STRATEGIES = set(["delete+insert", "insert_overwrite"])
INCREMENTAL_BY_UNIQUE_KEY_STRATEGIES = set(["merge"])


def collection_to_str(collection: t.Iterable) -> str:
    return ", ".join(f"'{item}'" for item in collection)


class ModelConfig(BaseModelConfig):
    """
    ModelConfig contains all config parameters available to DBT models

    See https://docs.getdbt.com/reference/configs-and-properties for
    a more detailed description of each config parameter under the
    General propreties, General configs, and For models sections.

    Args:
        sql: The model sql
        time_column: The name of the time column
        cron: A cron string specifying how often the model should be refreshed, leveraging the
            [croniter](https://github.com/kiorky/croniter) library.
        interval_unit: The duration of an interval for the model. By default, it is computed from the cron expression.
        batch_size: The maximum number of incremental intervals that can be run per backfill job. If this is None,
            then backfilling this model will do all of history in one job. If this is set, a model's backfill
            will be chunked such that each individual job will only contain jobs with max `batch_size` intervals.
        lookback: The number of previous incremental intervals in the lookback window.
        cluster_by: Field(s) to use for clustering in data warehouses that support clustering
        incremental_strategy: Strategy used to build the incremental model
        materialized: How the model will be materialized in the database
        sql_header: SQL statement to run before table/view creation. Currently implemented as a pre-hook.
        unique_key: List of columns that define row uniqueness for the model
        partition_by: List of partition columns or dictionary of bigquery partition by parameters ([dbt bigquery config](https://docs.getdbt.com/reference/resource-configs/bigquery-configs)).
    """

    # sqlmesh fields
    sql: SqlStr = SqlStr("")
    time_column: t.Optional[str] = None
    cron: t.Optional[str] = None
    interval_unit: t.Optional[str] = None
    batch_size: t.Optional[int] = None
    lookback: t.Optional[int] = None
    forward_only: bool = True
    disable_restatement: t.Optional[bool] = None
    allow_partials: t.Optional[bool] = None

    # DBT configuration fields
    cluster_by: t.Optional[t.List[str]] = None
    incremental_strategy: t.Optional[str] = None
    materialized: str = Materialization.VIEW.value
    sql_header: t.Optional[str] = None
    unique_key: t.Optional[t.List[str]] = None
    partition_by: t.Optional[t.Union[t.List[str], t.Dict[str, t.Any]]] = None

    # Snapshot (SCD Type 2) Fields
    updated_at: t.Optional[str] = None
    strategy: t.Optional[str] = None
    invalidate_hard_deletes: bool = False
    target_schema: t.Optional[str] = None
    check_cols: t.Optional[t.Union[t.List[str], str]] = None

    # redshift
    bind: t.Optional[bool] = None

    # bigquery
    require_partition_filter: t.Optional[bool] = None
    partition_expiration_days: t.Optional[int] = None

    # snowflake
    snowflake_warehouse: t.Optional[str] = None

    # Private fields
    _sql_embedded_config: t.Optional[SqlStr] = None
    _sql_no_config: t.Optional[SqlStr] = None

    _sql_validator = sql_str_validator

    @field_validator(
        "unique_key",
        "cluster_by",
        "tags",
        mode="before",
    )
    @classmethod
    def _validate_list(cls, v: t.Union[str, t.List[str]]) -> t.List[str]:
        return ensure_list(v)

    @field_validator("check_cols", mode="before")
    @classmethod
    def _validate_check_cols(cls, v: t.Union[str, t.List[str]]) -> t.Union[str, t.List[str]]:
        if isinstance(v, str) and v.lower() == "all":
            return "*"
        return ensure_list(v)

    @field_validator("sql", mode="before")
    @classmethod
    def _validate_sql(cls, v: t.Union[str, SqlStr]) -> SqlStr:
        return SqlStr(v)

    @field_validator("partition_by", mode="before")
    @classmethod
    def _validate_partition_by(cls, v: t.Any) -> t.Union[t.List[str], t.Dict[str, t.Any]]:
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            if not v.get("field"):
                raise ConfigError("'field' key required for partition_by.")
            if "granularity" in v and v["granularity"].lower() not in (
                "day",
                "month",
                "year",
                "hour",
            ):
                granularity = v["granularity"]
                raise ConfigError(f"Unexpected granularity '{granularity}' in partition_by '{v}'.")
            return {"data_type": "date", "granularity": "day", **v}
        raise ConfigError(f"Invalid format for partition_by '{v}'")

    _FIELD_UPDATE_STRATEGY: t.ClassVar[t.Dict[str, UpdateStrategy]] = {
        **BaseModelConfig._FIELD_UPDATE_STRATEGY,
        **{
            "sql": UpdateStrategy.IMMUTABLE,
            "time_column": UpdateStrategy.IMMUTABLE,
        },
    }

    @property
    def model_materialization(self) -> Materialization:
        return Materialization(self.materialized.lower())

    @property
    def snapshot_strategy(self) -> t.Optional[SnapshotStrategy]:
        return SnapshotStrategy(self.strategy.lower()) if self.strategy else None

    @property
    def table_schema(self) -> str:
        return self.target_schema or super().table_schema

    def model_kind(self, context: DbtContext) -> ModelKind:
        """
        Get the sqlmesh ModelKind
        Returns:
            The sqlmesh ModelKind
        """
        target = context.target
        materialization = self.model_materialization
        if materialization == Materialization.TABLE:
            return FullKind()
        if materialization == Materialization.VIEW:
            return ViewKind()
        if materialization == Materialization.INCREMENTAL:
            incremental_kwargs = {"dialect": context.dialect}
            for field in ("batch_size", "lookback", "forward_only", "disable_restatement"):
                field_val = getattr(self, field, None) or self.meta.get(field, None)
                if field_val:
                    incremental_kwargs[field] = field_val

            if self.time_column:
                strategy = self.incremental_strategy or target.default_incremental_strategy(
                    IncrementalByTimeRangeKind
                )

                if strategy not in INCREMENTAL_BY_TIME_STRATEGIES:
                    logger.warning(
                        "SQLMesh incremental by time strategy is not compatible with '%s' incremental strategy in model '%s'. Supported strategies include %s.",
                        strategy,
                        self.canonical_name(context),
                        collection_to_str(INCREMENTAL_BY_TIME_STRATEGIES),
                    )

                return IncrementalByTimeRangeKind(
                    time_column=self.time_column,
                    **incremental_kwargs,
                )

            if self.unique_key:
                strategy = self.incremental_strategy or target.default_incremental_strategy(
                    IncrementalByUniqueKeyKind
                )
                if (
                    self.incremental_strategy
                    and strategy not in INCREMENTAL_BY_UNIQUE_KEY_STRATEGIES
                ):
                    raise ConfigError(
                        f"{self.canonical_name(context)}: SQLMesh incremental by unique key strategy is not compatible with '{strategy}'"
                        f" incremental strategy. Supported strategies include {collection_to_str(INCREMENTAL_BY_UNIQUE_KEY_STRATEGIES)}."
                    )
                return IncrementalByUniqueKeyKind(unique_key=self.unique_key, **incremental_kwargs)

            logger.warning(
                "Using unmanaged incremental materialization for model '%s'. Some features might not be available. Consider adding either a time_column (%s) or a unique_key (%s) configuration to mitigate this",
                self.canonical_name(context),
                collection_to_str(INCREMENTAL_BY_TIME_STRATEGIES),
                collection_to_str(INCREMENTAL_BY_UNIQUE_KEY_STRATEGIES.union(["none"])),
            )
            strategy = self.incremental_strategy or target.default_incremental_strategy(
                IncrementalUnmanagedKind
            )
            return IncrementalUnmanagedKind(
                insert_overwrite=strategy in INCREMENTAL_BY_TIME_STRATEGIES,
                forward_only=incremental_kwargs.get("forward_only", True),
            )
        if materialization == Materialization.EPHEMERAL:
            return EmbeddedKind()
        if materialization == Materialization.SNAPSHOT:
            if not self.snapshot_strategy:
                raise ConfigError(
                    f"{self.canonical_name(context)}: SQLMesh snapshot strategy is required for snapshot materialization."
                )
            shared_kwargs = {
                "dialect": context.dialect,
                "unique_key": self.unique_key,
                "invalidate_hard_deletes": self.invalidate_hard_deletes,
                "valid_from_name": "dbt_valid_from",
                "valid_to_name": "dbt_valid_to",
                "time_data_type": (
                    exp.DataType.build("TIMESTAMPTZ")
                    if target.dialect == "bigquery"
                    else exp.DataType.build("TIMESTAMP")
                ),
            }
            if self.snapshot_strategy.is_check:
                return SCDType2ByColumnKind(
                    columns=self.check_cols, execution_time_as_valid_from=True, **shared_kwargs
                )
            return SCDType2ByTimeKind(
                updated_at_name=self.updated_at, updated_at_as_valid_from=True, **shared_kwargs
            )
        raise ConfigError(f"{materialization.value} materialization not supported.")

    @property
    def sql_no_config(self) -> SqlStr:
        if self._sql_no_config is None:
            self._sql_no_config = SqlStr("")
            self._extract_sql_config()
        return self._sql_no_config

    @property
    def sql_embedded_config(self) -> SqlStr:
        if self._sql_embedded_config is None:
            self._sql_embedded_config = SqlStr("")
            self._extract_sql_config()
        return self._sql_embedded_config

    def _extract_sql_config(self) -> None:
        no_config, embedded_config = extract_jinja_config(self.sql)
        self._sql_no_config = SqlStr(no_config)
        self._sql_embedded_config = SqlStr(embedded_config)

    def _big_query_partition_by_expr(self, context: DbtContext) -> exp.Expression:
        assert isinstance(self.partition_by, dict)
        data_type = self.partition_by["data_type"].lower()
        field = d.parse_one(self.partition_by["field"], dialect="bigquery")
        if data_type == "date" and self.partition_by["granularity"].lower() == "day":
            return field

        if data_type == "int64":
            if "range" not in self.partition_by:
                raise ConfigError(
                    f"Range is required for int64 partitioning in model '{self.canonical_name(context)}'."
                )

            range_ = self.partition_by["range"]
            start = range_["start"]
            end = range_["end"]
            interval = range_["interval"]

            return exp.func(
                "RANGE_BUCKET",
                field,
                exp.func("GENERATE_ARRAY", start, end, interval, dialect="bigquery"),
                dialect="bigquery",
            )

        return d.parse_one(
            f"{data_type}_trunc({self.partition_by['field']}, {self.partition_by['granularity'].upper()})",
            dialect="bigquery",
        )

    @property
    def sqlmesh_config_fields(self) -> t.Set[str]:
        return super().sqlmesh_config_fields | {"cron", "interval_unit", "allow_partials"}

    def to_sqlmesh(self, context: DbtContext) -> Model:
        """Converts the dbt model into a SQLMesh model."""
        dialect = context.dialect
        query = d.jinja_query(self.sql_no_config)

        optional_kwargs: t.Dict[str, t.Any] = {}

        if self.partition_by:
            optional_kwargs["partitioned_by"] = (
                [exp.to_column(val) for val in self.partition_by]
                if isinstance(self.partition_by, list)
                else self._big_query_partition_by_expr(context)
            )

        if self.cluster_by:
            optional_kwargs["clustered_by"] = [
                d.parse_one(c, dialect=dialect).name for c in self.cluster_by
            ]

        model_kwargs = self.sqlmesh_model_kwargs(context)
        if self.sql_header:
            model_kwargs["pre_statements"].insert(0, d.jinja_statement(self.sql_header))

        if context.target.dialect == "bigquery":
            dbt_max_partition_blob = self._dbt_max_partition_blob()
            if dbt_max_partition_blob:
                model_kwargs["pre_statements"].append(d.jinja_statement(dbt_max_partition_blob))

            table_properties = {}
            if self.partition_expiration_days is not None:
                table_properties["partition_expiration_days"] = self.partition_expiration_days
            if self.require_partition_filter is not None:
                table_properties["require_partition_filter"] = self.require_partition_filter

            if table_properties:
                model_kwargs["table_properties"] = table_properties

        if context.target.dialect == "snowflake" and self.snowflake_warehouse is not None:
            model_kwargs["session_properties"] = {"warehouse": self.snowflake_warehouse}

        return create_sql_model(
            self.canonical_name(context),
            query,
            dialect=dialect,
            kind=self.model_kind(context),
            start=self.start,
            **optional_kwargs,
            **model_kwargs,
        )

    def _dbt_max_partition_blob(self) -> t.Optional[str]:
        """Returns a SQL blob which declares the _dbt_max_partition variable. Only applicable to BigQuery."""
        if (
            not isinstance(self.partition_by, dict)
            or self.model_materialization != Materialization.INCREMENTAL
        ):
            return None

        from sqlmesh.core.engine_adapter.bigquery import select_partitions_expr

        data_type = self.partition_by["data_type"]
        select_max_partition_expr = select_partitions_expr(
            "{{ adapter.resolve_schema(this) }}",
            "{{ adapter.resolve_identifier(this) }}",
            data_type,
            granularity=self.partition_by.get("granularity"),
            database="{{ target.database }}",
        )

        data_type = data_type.upper()
        default_value = "NULL"
        if data_type in ("DATE", "DATETIME", "TIMESTAMP"):
            default_value = f"CAST('1970-01-01' AS {data_type})"

        return f"""
{{% if is_incremental() %}}
  DECLARE _dbt_max_partition {data_type} DEFAULT (
    COALESCE(({select_max_partition_expr}), {default_value})
  );
{{% endif %}}
"""
