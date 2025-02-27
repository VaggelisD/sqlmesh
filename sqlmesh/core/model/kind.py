from __future__ import annotations

import sys
import typing as t
from enum import Enum

from pydantic import Field
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify_columns import quote_identifiers
from sqlglot.optimizer.simplify import gen
from sqlglot.time import format_time

from sqlmesh.core import dialect as d
from sqlmesh.core.model.common import parse_properties
from sqlmesh.core.model.seed import CsvSettings
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import (
    PydanticModel,
    SQLGlotBool,
    SQLGlotListOfColumnsOrStar,
    SQLGlotListOfFields,
    SQLGlotPositiveInt,
    SQLGlotString,
    field_validator,
    field_validator_v1_args,
    model_validator,
    model_validator_v1_args,
)

if sys.version_info >= (3, 9):
    from typing import Annotated, Literal
else:
    from typing_extensions import Annotated, Literal


if t.TYPE_CHECKING:
    MODEL_KIND = t.TypeVar("MODEL_KIND", bound="_ModelKind")


class ModelKindMixin:
    @property
    def model_kind_name(self) -> t.Optional[ModelKindName]:
        """Returns the model kind name."""
        raise NotImplementedError

    @property
    def is_incremental_by_time_range(self) -> bool:
        return self.model_kind_name == ModelKindName.INCREMENTAL_BY_TIME_RANGE

    @property
    def is_incremental_by_unique_key(self) -> bool:
        return self.model_kind_name == ModelKindName.INCREMENTAL_BY_UNIQUE_KEY

    @property
    def is_incremental_unmanaged(self) -> bool:
        return self.model_kind_name == ModelKindName.INCREMENTAL_UNMANAGED

    @property
    def is_incremental(self) -> bool:
        return (
            self.is_incremental_by_time_range
            or self.is_incremental_by_unique_key
            or self.is_incremental_unmanaged
            or self.is_scd_type_2
        )

    @property
    def is_full(self) -> bool:
        return self.model_kind_name == ModelKindName.FULL

    @property
    def is_view(self) -> bool:
        return self.model_kind_name == ModelKindName.VIEW

    @property
    def is_embedded(self) -> bool:
        return self.model_kind_name == ModelKindName.EMBEDDED

    @property
    def is_seed(self) -> bool:
        return self.model_kind_name == ModelKindName.SEED

    @property
    def is_external(self) -> bool:
        return self.model_kind_name == ModelKindName.EXTERNAL

    @property
    def is_scd_type_2(self) -> bool:
        return self.model_kind_name in {
            ModelKindName.SCD_TYPE_2,
            ModelKindName.SCD_TYPE_2_BY_TIME,
            ModelKindName.SCD_TYPE_2_BY_COLUMN,
        }

    @property
    def is_scd_type_2_by_time(self) -> bool:
        return self.model_kind_name in {ModelKindName.SCD_TYPE_2, ModelKindName.SCD_TYPE_2_BY_TIME}

    @property
    def is_scd_type_2_by_column(self) -> bool:
        return self.model_kind_name == ModelKindName.SCD_TYPE_2_BY_COLUMN

    @property
    def is_symbolic(self) -> bool:
        """A symbolic model is one that doesn't execute at all."""
        return self.model_kind_name in (ModelKindName.EMBEDDED, ModelKindName.EXTERNAL)

    @property
    def is_materialized(self) -> bool:
        return not (self.is_symbolic or self.is_view)

    @property
    def only_execution_time(self) -> bool:
        """Whether or not this model only cares about execution time to render."""
        return self.is_view or self.is_full or self.is_scd_type_2


class ModelKindName(str, ModelKindMixin, Enum):
    """The kind of model, determining how this data is computed and stored in the warehouse."""

    INCREMENTAL_BY_TIME_RANGE = "INCREMENTAL_BY_TIME_RANGE"
    INCREMENTAL_BY_UNIQUE_KEY = "INCREMENTAL_BY_UNIQUE_KEY"
    INCREMENTAL_UNMANAGED = "INCREMENTAL_UNMANAGED"
    FULL = "FULL"
    # Legacy alias to SCD Type 2 By Time
    # Only used for Parsing and mapping name to SCD Type 2 By Time
    SCD_TYPE_2 = "SCD_TYPE_2"
    SCD_TYPE_2_BY_TIME = "SCD_TYPE_2_BY_TIME"
    SCD_TYPE_2_BY_COLUMN = "SCD_TYPE_2_BY_COLUMN"
    VIEW = "VIEW"
    EMBEDDED = "EMBEDDED"
    SEED = "SEED"
    EXTERNAL = "EXTERNAL"

    @property
    def model_kind_name(self) -> t.Optional[ModelKindName]:
        return self

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return str(self)


class _ModelKind(PydanticModel, ModelKindMixin):
    name: ModelKindName

    @property
    def model_kind_name(self) -> t.Optional[ModelKindName]:
        return self.name

    def to_expression(self, **kwargs: t.Any) -> d.ModelKind:
        return d.ModelKind(this=self.name.value.upper(), **kwargs)

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [self.name.value]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return []


class TimeColumn(PydanticModel):
    column: exp.Expression
    format: t.Optional[str] = None

    @classmethod
    def validator(cls) -> classmethod:
        def _time_column_validator(v: t.Any, values: t.Any) -> TimeColumn:
            values = values if isinstance(values, dict) else values.data
            dialect = values.get("dialect")
            if isinstance(v, exp.Tuple):
                column_expr = v.expressions[0]
                column = (
                    exp.column(column_expr)
                    if isinstance(column_expr, exp.Identifier)
                    else column_expr
                )
                format = v.expressions[1].name if len(v.expressions) > 1 else None
            elif isinstance(v, exp.Expression):
                column = exp.column(v) if isinstance(v, exp.Identifier) else v
                format = None
            elif isinstance(v, str):
                column = d.parse_one(v, dialect=dialect)
                column.meta.pop("sql")
                format = None
            elif isinstance(v, dict):
                column_raw = v["column"]
                column = (
                    d.parse_one(column_raw, dialect=dialect)
                    if isinstance(column_raw, str)
                    else column_raw
                )
                format = v.get("format")
            elif isinstance(v, TimeColumn):
                return v
            else:
                raise ConfigError(f"Invalid time_column: '{v}'.")

            column = quote_identifiers(
                normalize_identifiers(column, dialect=dialect), dialect=dialect
            )
            column.meta["dialect"] = dialect

            return TimeColumn(column=column, format=format)

        return field_validator("time_column", mode="before")(_time_column_validator)

    @field_validator("column", mode="before")
    @classmethod
    def _column_validator(cls, v: t.Union[str, exp.Expression]) -> exp.Expression:
        if not v:
            raise ConfigError("Time Column cannot be empty.")
        if isinstance(v, str):
            return exp.to_column(v)
        return v

    @property
    def expression(self) -> exp.Expression:
        """Convert this pydantic model into a time_column SQLGlot expression."""
        if not self.format:
            return self.column

        return exp.Tuple(expressions=[self.column, exp.Literal.string(self.format)])

    def to_expression(self, dialect: str) -> exp.Expression:
        """Convert this pydantic model into a time_column SQLGlot expression."""
        if not self.format:
            return self.column

        return exp.Tuple(
            expressions=[
                self.column,
                exp.Literal.string(
                    format_time(self.format, d.Dialect.get_or_raise(dialect).INVERSE_TIME_MAPPING)
                ),
            ]
        )

    def to_property(self, dialect: str = "") -> exp.Property:
        return exp.Property(this="time_column", value=self.to_expression(dialect))


class _Incremental(_ModelKind):
    dialect: str = ""
    batch_size: t.Optional[SQLGlotPositiveInt] = None
    lookback: t.Optional[SQLGlotPositiveInt] = None
    forward_only: SQLGlotBool = False
    disable_restatement: SQLGlotBool = False

    @model_validator(mode="after")
    @model_validator_v1_args
    def _kind_validator(cls, values: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
        batch_size = values.get("batch_size")
        lookback = values.get("lookback") or 0
        if batch_size and batch_size < lookback:
            raise ValueError("batch_size cannot be less than lookback")
        return values

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            self.dialect,
            str(self.lookback) if self.lookback is not None else None,
        ]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.batch_size) if self.batch_size is not None else None,
            str(self.forward_only),
            str(self.disable_restatement),
        ]


class IncrementalByTimeRangeKind(_Incremental):
    name: Literal[ModelKindName.INCREMENTAL_BY_TIME_RANGE] = ModelKindName.INCREMENTAL_BY_TIME_RANGE
    time_column: TimeColumn

    _time_column_validator = TimeColumn.validator()

    def to_expression(self, dialect: str = "", **kwargs: t.Any) -> d.ModelKind:
        return super().to_expression(expressions=[self.time_column.to_property(dialect)])

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().data_hash_values, gen(self.time_column.column), self.time_column.format]


class IncrementalByUniqueKeyKind(_Incremental):
    name: Literal[ModelKindName.INCREMENTAL_BY_UNIQUE_KEY] = ModelKindName.INCREMENTAL_BY_UNIQUE_KEY
    unique_key: SQLGlotListOfFields
    when_matched: t.Optional[exp.When] = None

    @field_validator("when_matched", mode="before")
    @field_validator_v1_args
    def _when_matched_validator(
        cls, v: t.Optional[t.Union[exp.When, str]], values: t.Dict[str, t.Any]
    ) -> t.Optional[exp.When]:
        def replace_table_references(expression: exp.Expression) -> exp.Expression:
            from sqlmesh.core.engine_adapter.base import (
                MERGE_SOURCE_ALIAS,
                MERGE_TARGET_ALIAS,
            )

            if isinstance(expression, exp.Column):
                if expression.table.lower() == "target":
                    expression.set(
                        "table",
                        exp.to_identifier(MERGE_TARGET_ALIAS),
                    )
                elif expression.table.lower() == "source":
                    expression.set(
                        "table",
                        exp.to_identifier(MERGE_SOURCE_ALIAS),
                    )
            return expression

        if isinstance(v, str):
            return t.cast(exp.When, d.parse_one(v, into=exp.When))

        if not v:
            return v

        return t.cast(exp.When, v.transform(replace_table_references))

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            *(gen(k) for k in self.unique_key),
            gen(self.when_matched) if self.when_matched is not None else None,
        ]


class IncrementalUnmanagedKind(_ModelKind):
    name: Literal[ModelKindName.INCREMENTAL_UNMANAGED] = ModelKindName.INCREMENTAL_UNMANAGED
    insert_overwrite: SQLGlotBool = False
    forward_only: SQLGlotBool = True
    disable_restatement: Literal[True] = True

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().data_hash_values, str(self.insert_overwrite)]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().metadata_hash_values, str(self.forward_only)]


class ViewKind(_ModelKind):
    name: Literal[ModelKindName.VIEW] = ModelKindName.VIEW
    materialized: SQLGlotBool = False

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().data_hash_values, str(self.materialized)]


class SeedKind(_ModelKind):
    name: Literal[ModelKindName.SEED] = ModelKindName.SEED
    path: SQLGlotString
    batch_size: SQLGlotPositiveInt = 1000
    csv_settings: t.Optional[CsvSettings] = None

    @field_validator("csv_settings", mode="before")
    @classmethod
    def _parse_csv_settings(cls, v: t.Any) -> t.Optional[CsvSettings]:
        if v is None or isinstance(v, CsvSettings):
            return v
        if isinstance(v, exp.Expression):
            tuple_exp = parse_properties(cls, v, {})
            if not tuple_exp:
                return None
            return CsvSettings(**{e.left.name: e.right for e in tuple_exp.expressions})
        if isinstance(v, dict):
            return CsvSettings(**v)
        return v

    def to_expression(self, **kwargs: t.Any) -> d.ModelKind:
        """Convert the seed kind into a SQLGlot expression."""
        return super().to_expression(
            expressions=[
                exp.Property(this=exp.Var(this="path"), value=exp.Literal.string(self.path)),
                exp.Property(
                    this=exp.Var(this="batch_size"),
                    value=exp.Literal.number(self.batch_size),
                ),
            ],
        )

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            *(self.csv_settings or CsvSettings()).dict().values(),
        ]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().metadata_hash_values, str(self.batch_size)]


class FullKind(_ModelKind):
    name: Literal[ModelKindName.FULL] = ModelKindName.FULL


class _SCDType2Kind(_ModelKind):
    dialect: str = ""
    unique_key: SQLGlotListOfFields
    valid_from_name: SQLGlotString = "valid_from"
    valid_to_name: SQLGlotString = "valid_to"
    invalidate_hard_deletes: SQLGlotBool = False
    time_data_type: exp.DataType = Field(exp.DataType.build("TIMESTAMP"), validate_default=True)

    forward_only: SQLGlotBool = True
    disable_restatement: SQLGlotBool = True

    # always=True can be removed once Pydantic 1 is deprecated
    @field_validator("time_data_type", mode="before", always=True)
    @classmethod
    def _time_data_type_validator(
        cls, v: t.Union[str, exp.Expression], values: t.Any
    ) -> exp.Expression:
        values = values if isinstance(values, dict) else values.data
        if isinstance(v, exp.Expression) and not isinstance(v, exp.DataType):
            v = v.name
        dialect = values.get("dialect")
        data_type = exp.DataType.build(v, dialect=dialect)
        data_type.meta["dialect"] = dialect
        return data_type

    @property
    def managed_columns(self) -> t.Dict[str, exp.DataType]:
        return {
            self.valid_from_name: self.time_data_type,
            self.valid_to_name: self.time_data_type,
        }

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            self.dialect,
            *(gen(k) for k in self.unique_key),
            self.valid_from_name,
            self.valid_to_name,
            str(self.invalidate_hard_deletes),
            gen(self.time_data_type),
        ]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.forward_only),
            str(self.disable_restatement),
        ]


class SCDType2ByTimeKind(_SCDType2Kind):
    name: Literal[ModelKindName.SCD_TYPE_2, ModelKindName.SCD_TYPE_2_BY_TIME] = (
        ModelKindName.SCD_TYPE_2_BY_TIME
    )
    updated_at_name: SQLGlotString = "updated_at"
    updated_at_as_valid_from: SQLGlotBool = False

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().data_hash_values, self.updated_at_name, str(self.updated_at_as_valid_from)]


class SCDType2ByColumnKind(_SCDType2Kind):
    name: Literal[ModelKindName.SCD_TYPE_2_BY_COLUMN] = ModelKindName.SCD_TYPE_2_BY_COLUMN
    columns: SQLGlotListOfColumnsOrStar
    execution_time_as_valid_from: SQLGlotBool = False

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        columns_sql = (
            [gen(c) for c in self.columns]
            if isinstance(self.columns, list)
            else [gen(self.columns)]
        )
        return [*super().data_hash_values, *columns_sql, str(self.execution_time_as_valid_from)]


class EmbeddedKind(_ModelKind):
    name: Literal[ModelKindName.EMBEDDED] = ModelKindName.EMBEDDED


class ExternalKind(_ModelKind):
    name: Literal[ModelKindName.EXTERNAL] = ModelKindName.EXTERNAL


ModelKind = Annotated[
    t.Union[
        EmbeddedKind,
        ExternalKind,
        FullKind,
        IncrementalByTimeRangeKind,
        IncrementalByUniqueKeyKind,
        IncrementalUnmanagedKind,
        SeedKind,
        ViewKind,
        SCDType2ByTimeKind,
        SCDType2ByColumnKind,
    ],
    Field(discriminator="name"),
]

MODEL_KIND_NAME_TO_TYPE: t.Dict[str, t.Type[ModelKind]] = {
    ModelKindName.EMBEDDED: EmbeddedKind,
    ModelKindName.EXTERNAL: ExternalKind,
    ModelKindName.FULL: FullKind,
    ModelKindName.INCREMENTAL_BY_TIME_RANGE: IncrementalByTimeRangeKind,
    ModelKindName.INCREMENTAL_BY_UNIQUE_KEY: IncrementalByUniqueKeyKind,
    ModelKindName.INCREMENTAL_UNMANAGED: IncrementalUnmanagedKind,
    ModelKindName.SEED: SeedKind,
    ModelKindName.VIEW: ViewKind,
    ModelKindName.SCD_TYPE_2: SCDType2ByTimeKind,
    ModelKindName.SCD_TYPE_2_BY_TIME: SCDType2ByTimeKind,
    ModelKindName.SCD_TYPE_2_BY_COLUMN: SCDType2ByColumnKind,
}


def model_kind_type_from_name(name: t.Optional[str]) -> t.Type[ModelKind]:
    klass = MODEL_KIND_NAME_TO_TYPE.get(name) if name else None
    if not klass:
        raise ConfigError(f"Invalid model kind '{name}'")
    return t.cast(t.Type[ModelKind], klass)


@field_validator_v1_args
def _model_kind_validator(cls: t.Type, v: t.Any, values: t.Dict[str, t.Any]) -> ModelKind:
    dialect = values.get("dialect")

    if isinstance(v, _ModelKind):
        return t.cast(ModelKind, v)

    if isinstance(v, (d.ModelKind, dict)):
        props = (
            {prop.name: prop.args.get("value") for prop in v.expressions}
            if isinstance(v, d.ModelKind)
            else v
        )
        name = v.this if isinstance(v, d.ModelKind) else props.get("name")
        # We want to ensure whatever name is provided to construct the class is the same name that will be
        # found inside the class itself in order to avoid a change during plan/apply for legacy aliases.
        # Ex: Pass in `SCD_TYPE_2` then we want to ensure we get `SCD_TYPE_2` as the kind name
        # instead of `SCD_TYPE_2_BY_TIME`.
        props["name"] = name
        kind_type = model_kind_type_from_name(name)
        if "dialect" in kind_type.all_fields() and props.get("dialect") is None:
            props["dialect"] = dialect
        return kind_type(**props)

    name = (v.name if isinstance(v, exp.Expression) else str(v)).upper()
    return model_kind_type_from_name(name)(name=name)  # type: ignore


model_kind_validator = field_validator("kind", mode="before")(_model_kind_validator)
