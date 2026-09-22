"""Canonical Common IR Data Models for Unified Qlik and Tableau to Fabric Migration."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SemanticRole(str, Enum):
    DIMENSION = "dimension"
    MEASURE = "measure"
    CALCULATED_COLUMN = "calculated_column"
    PARAMETER = "parameter"
    SET = "set"
    FILTER_FIELD = "filter_field"


class TableType(str, Enum):
    FACT = "fact"
    DIMENSION = "dimension"
    BRIDGE = "bridge"
    STAGING = "staging"
    CALCULATED = "calculated"
    QUERY = "query"


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class LineageIR:
    source_object: str = ""
    mapping_object: str = ""
    canonical_object: str = ""
    generation_object: str = ""
    fabric_object: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticIR:
    severity: str = "info"  # info, warning, error
    code: str = ""
    message: str = ""
    source_object: str = ""
    requires_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConfidenceIR:
    confidence_score: float = 100.0
    confidence_reasons: List[str] = field(default_factory=list)
    mapping_method: str = "deterministic"
    model_used: Optional[str] = None
    fallback_used: bool = False
    rule_used: Optional[str] = None
    validation_results: List[str] = field(default_factory=list)
    requires_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ColumnIR:
    name: str
    canonical_name: str
    datatype: str = "string"
    nullable: bool = True
    source_type: str = "string"
    semantic_role: str = "dimension"
    expression: Optional[str] = None
    source_expression: Optional[str] = None
    lineage: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FieldIR:
    field_id: str
    source_platform: str
    source_table: str
    source_field: str
    canonical_table: str
    canonical_column: str
    datatype: str = "string"
    semantic_role: str = "dimension"
    lineage: List[str] = field(default_factory=list)
    confidence: float = 100.0
    requires_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TableIR:
    table_id: str
    source_name: str
    canonical_name: str
    table_type: str = "dimension"
    columns: List[ColumnIR] = field(default_factory=list)
    source_lineage: List[str] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    m_query: Optional[str] = None
    confidence: float = 100.0
    diagnostics: List[DiagnosticIR] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        res = asdict(self)
        res["columns"] = [c.to_dict() if isinstance(c, ColumnIR) else c for c in self.columns]
        res["diagnostics"] = [d.to_dict() if isinstance(d, DiagnosticIR) else d for d in self.diagnostics]
        return res


@dataclass
class MeasureIR:
    name: str
    expression: str
    expression_language: str = "DAX"
    source_expression: str = ""
    source_platform: str = "unknown"
    mapping_method: str = "deterministic"
    confidence_score: float = 100.0
    requires_review: bool = False
    lineage: List[str] = field(default_factory=list)
    formatting: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    table_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CalculatedColumnIR:
    name: str
    expression: str
    table_name: str
    expression_language: str = "DAX"
    source_expression: str = ""
    datatype: str = "string"
    dependencies: List[str] = field(default_factory=list)
    lineage: List[str] = field(default_factory=list)
    confidence: float = 100.0
    requires_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FilterIR:
    field: str
    operator: str = "equal"
    value: Any = None
    values: List[Any] = field(default_factory=list)
    scope: str = "visual"  # visual, sheet, report
    source_expression: Optional[str] = None
    dax_expression: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParameterIR:
    name: str
    datatype: str = "string"
    default_value: Any = None
    allowed_values: List[Any] = field(default_factory=list)
    min_value: Any = None
    max_value: Any = None
    source_expression: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SetIR:
    set_id: str
    name: str
    field: str
    operation: str = "include"  # include, exclude, condition
    values: List[Any] = field(default_factory=list)
    expression: Optional[str] = None
    source_platform: str = "unknown"
    requires_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RelationshipIR:
    relationship_id: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str = "many_to_one"  # many_to_one, one_to_many, one_to_one, many_to_many
    cross_filter: str = "single"      # single, both
    active: bool = True
    source_platform: str = "unknown"
    confidence_score: float = 100.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VisualIR:
    visual_id: str
    type: str
    title: str = ""
    dimensions: List[str] = field(default_factory=list)
    measures: List[str] = field(default_factory=list)
    encoding: Dict[str, Any] = field(default_factory=dict)
    filters: List[FilterIR] = field(default_factory=list)
    sorting: List[Dict[str, Any]] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)
    coordinates: Dict[str, Any] = field(default_factory=dict)
    drilldowns: List[str] = field(default_factory=list)
    source_sheet: str = ""
    confidence: float = 100.0
    diagnostics: List[DiagnosticIR] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        res = asdict(self)
        res["filters"] = [f.to_dict() if isinstance(f, FilterIR) else f for f in self.filters]
        res["diagnostics"] = [d.to_dict() if isinstance(d, DiagnosticIR) else d for d in self.diagnostics]
        return res


@dataclass
class SecurityIR:
    type: str = "rls"
    roles: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "unknown"
    review_required: bool = False
    unsupported_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CanonicalContract2:
    contract_version: str = "2.0"
    status: str = "success"
    message: str = "Contract 2.0 generated successfully"
    error_message: Optional[str] = None
    workbook_metadata: Dict[str, Any] = field(default_factory=dict)
    app_metadata: Dict[str, Any] = field(default_factory=dict)
    app_layout: Dict[str, Any] = field(default_factory=dict)
    connections: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[TableIR] = field(default_factory=list)
    columns: List[ColumnIR] = field(default_factory=list)
    dimensions: List[Dict[str, Any]] = field(default_factory=list)
    measures: List[MeasureIR] = field(default_factory=list)
    calculated_columns: List[CalculatedColumnIR] = field(default_factory=list)
    custom_sql: List[Dict[str, Any]] = field(default_factory=list)
    visuals: List[VisualIR] = field(default_factory=list)
    filters: List[FilterIR] = field(default_factory=list)
    parameters: List[ParameterIR] = field(default_factory=list)
    sets: List[SetIR] = field(default_factory=list)
    relationships: List[RelationshipIR] = field(default_factory=list)
    security: SecurityIR = field(default_factory=SecurityIR)
    lineage: List[LineageIR] = field(default_factory=list)
    diagnostics: List[DiagnosticIR] = field(default_factory=list)
    limitations: List[Dict[str, Any]] = field(default_factory=list)
    variables: List[Dict[str, Any]] = field(default_factory=list)
    section_access: Optional[Dict[str, Any]] = None
    stories: List[Dict[str, Any]] = field(default_factory=list)
    bookmarks: List[Dict[str, Any]] = field(default_factory=list)
    themes: List[Dict[str, Any]] = field(default_factory=list)
    extensions: List[Dict[str, Any]] = field(default_factory=list)
    master_item_tags: List[Dict[str, Any]] = field(default_factory=list)
    hypercube_samples: List[Dict[str, Any]] = field(default_factory=list)
    script: Optional[str] = None
    data_load_editor: Dict[str, Any] = field(default_factory=dict)
    fields: List[FieldIR] = field(default_factory=list)
    rls: Dict[str, Any] = field(default_factory=dict)
    data_model: Dict[str, Any] = field(default_factory=dict)
    limitations_summary: Dict[str, Any] = field(default_factory=dict)
    object_inventory: Dict[str, Any] = field(default_factory=dict)
    section_status: Dict[str, Any] = field(default_factory=dict)
    extraction: Dict[str, Any] = field(default_factory=dict)
    master_objects: List[Dict[str, Any]] = field(default_factory=list)
    media: List[Dict[str, Any]] = field(default_factory=list)
    snapshots: List[Dict[str, Any]] = field(default_factory=list)
    data_files: List[Dict[str, Any]] = field(default_factory=list)
    conversion_summary: Dict[str, Any] = field(default_factory=dict)
    llm_status: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Produce the standard 41-key Contract 2.0 dictionary."""
        tables_dicts = [t.to_dict() if isinstance(t, TableIR) else t for t in self.tables]
        columns_dicts = [c.to_dict() if isinstance(c, ColumnIR) else c for c in self.columns]
        measures_dicts = [m.to_dict() if isinstance(m, MeasureIR) else m for m in self.measures]
        calc_cols_dicts = [cc.to_dict() if isinstance(cc, CalculatedColumnIR) else cc for cc in self.calculated_columns]
        visuals_dicts = [v.to_dict() if isinstance(v, VisualIR) else v for v in self.visuals]
        filters_dicts = [f.to_dict() if isinstance(f, FilterIR) else f for f in self.filters]
        params_dicts = [p.to_dict() if isinstance(p, ParameterIR) else p for p in self.parameters]
        sets_dicts = [s.to_dict() if isinstance(s, SetIR) else s for s in self.sets]
        rels_dicts = [r.to_dict() if isinstance(r, RelationshipIR) else r for r in self.relationships]
        fields_dicts = [f.to_dict() if isinstance(f, FieldIR) else f for f in self.fields]
        lineage_dicts = [l.to_dict() if isinstance(l, LineageIR) else l for l in self.lineage]
        diag_dicts = [d.to_dict() if isinstance(d, DiagnosticIR) else d for d in self.diagnostics]
        sec_dict = self.security.to_dict() if isinstance(self.security, SecurityIR) else self.security

        return {
            "status": self.status,
            "message": self.message,
            "error_message": self.error_message,
            "contract_version": self.contract_version,
            "workbook_metadata": self.workbook_metadata,
            "app_layout": self.app_layout,
            "app_metadata": self.app_metadata,
            "connections": self.connections,
            "tables": tables_dicts,
            "relationships": rels_dicts,
            "measures": measures_dicts,
            "dimensions": self.dimensions,
            "calculated_columns": calc_cols_dicts,
            "custom_sql": self.custom_sql,
            "visuals": visuals_dicts,
            "filters": filters_dicts,
            "limitations": self.limitations,
            "variables": self.variables,
            "section_access": self.section_access,
            "stories": self.stories,
            "bookmarks": self.bookmarks,
            "themes": self.themes,
            "extensions": self.extensions,
            "master_item_tags": self.master_item_tags,
            "hypercube_samples": self.hypercube_samples,
            "script": self.script,
            "data_load_editor": self.data_load_editor,
            "fields": fields_dicts,
            "rls": sec_dict if sec_dict else self.rls,
            "data_model": self.data_model or {
                "tables": tables_dicts,
                "relationships": rels_dicts,
                "measures": measures_dicts,
                "parameters": params_dicts,
                "sets": sets_dicts,
            },
            "lineage": lineage_dicts,
            "limitations_summary": self.limitations_summary,
            "object_inventory": self.object_inventory,
            "section_status": self.section_status,
            "extraction": self.extraction,
            "master_objects": self.master_objects,
            "media": self.media,
            "snapshots": self.snapshots,
            "data_files": self.data_files,
            "conversion_summary": self.conversion_summary,
            "llm_status": self.llm_status,
            "diagnostics": diag_dicts,
            "parameters": params_dicts,
            "sets": sets_dicts,
            "security": sec_dict,
        }
