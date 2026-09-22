"""Universal Source Model for Qlik, Tableau, and Future BI Source Ingestion.

Provides canonical, metadata-driven abstractions representing source applications,
schemas, tables, views, files, fields, measures, variables, parameters, mappings,
relationships, filters, visuals, and complete lineage.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class SourceType(str, Enum):
    QLIK = "qlik"
    TABLEAU = "tableau"
    POWER_BI = "power_bi"
    GENERIC = "generic"


class TableSourceType(str, Enum):
    PHYSICAL_CONNECTOR = "physical_connector"
    DATABASE_TABLE = "database_table"
    DATABASE_VIEW = "database_view"
    FILE = "file"
    SHEET = "sheet"
    INLINE = "inline"
    RESIDENT = "resident"
    MAPPING_TABLE = "mapping_table"
    GENERATED = "generated"
    CALCULATED = "calculated"
    DERIVED = "derived"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class DataType(str, Enum):
    STRING = "string"
    INTEGER = "int64"
    DOUBLE = "double"
    DATETIME = "dateTime"
    BOOLEAN = "boolean"
    BINARY = "binary"
    UNKNOWN = "unknown"


@dataclass
class UniversalSourceField:
    field_id: str
    name: str
    data_type: DataType = DataType.STRING
    table_name: Optional[str] = None
    source_expression: Optional[str] = None
    is_calculated: bool = False
    is_key: bool = False
    is_foreign_key: bool = False
    null_ratio: float = 0.0
    distinct_count: Optional[int] = None
    format_string: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalSourceTable:
    table_id: str
    name: str
    source_type: TableSourceType = TableSourceType.DATABASE_TABLE
    connection_id: Optional[str] = None
    connection_details: Dict[str, Any] = field(default_factory=dict)
    custom_sql: Optional[str] = None
    upstream_table: Optional[str] = None
    file_path: Optional[str] = None
    sheet_name: Optional[str] = None
    fields: List[UniversalSourceField] = field(default_factory=list)
    is_mapping_table: bool = False
    mapping_key_field: Optional[str] = None
    mapping_value_field: Optional[str] = None
    lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalMeasure:
    measure_id: str
    name: str
    source_expression: str
    target_dax: Optional[str] = None
    source_table: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    format_string: Optional[str] = "#,##0.00"
    is_calculated: bool = True
    confidence: float = 1.0
    requires_review: bool = False
    semantic_loss: bool = False
    lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalVariable:
    variable_id: str
    name: str
    definition: str
    evaluated_value: Optional[str] = None
    is_dynamic: bool = False
    dependencies: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)
    requires_review: bool = False
    lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalRelationship:
    relationship_id: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str = "manyToOne"
    cross_filter_direction: str = "single"
    is_active: bool = True
    confidence: float = 1.0
    lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalFilter:
    filter_id: str
    name: str
    source_expression: str
    target_table: Optional[str] = None
    target_column: Optional[str] = None
    target_dax: Optional[str] = None
    filter_scope: str = "report"
    dependencies: List[str] = field(default_factory=list)
    resolved: bool = True
    requires_review: bool = False
    lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalVisual:
    visual_id: str
    title: str
    source_chart_type: str
    target_fabric_type: str
    supported: bool = True
    field_roles: List[Dict[str, Any]] = field(default_factory=list)
    unbound_fields: List[str] = field(default_factory=list)
    layout: Dict[str, Any] = field(default_factory=dict)
    formatting: Dict[str, Any] = field(default_factory=dict)
    requires_review: bool = False
    semantic_loss: bool = False
    lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UniversalSourceModel:
    application_id: str
    application_name: str
    source_type: SourceType = SourceType.QLIK
    tenant: Optional[str] = None
    version: Optional[str] = None
    connections: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[UniversalSourceTable] = field(default_factory=list)
    measures: List[UniversalMeasure] = field(default_factory=list)
    variables: List[UniversalVariable] = field(default_factory=list)
    relationships: List[UniversalRelationship] = field(default_factory=list)
    filters: List[UniversalFilter] = field(default_factory=list)
    visuals: List[UniversalVisual] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_table(self, name: str) -> Optional[UniversalSourceTable]:
        for t in self.tables:
            if t.name.lower() == name.lower():
                return t
        return None

    def get_measure(self, name: str) -> Optional[UniversalMeasure]:
        for m in self.measures:
            if m.name.lower() == name.lower():
                return m
        return None
