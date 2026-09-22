"""Metadata Profiler for Source Schemas and Tables."""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from .source_model import DataType, UniversalSourceField, UniversalSourceTable


@dataclass
class ColumnProfile:
    column_name: str
    inferred_type: DataType
    is_candidate_key: bool = False
    is_foreign_key_candidate: bool = False
    is_numeric: bool = False
    is_date: bool = False
    distinct_count: Optional[int] = None
    null_ratio: float = 0.0


class SourceProfiler:
    """Profiles source columns and tables to extract semantic metadata without hardcoding."""

    DATE_PATTERNS = re.compile(r"(date|time|timestamp|created|updated|year|month|day)", re.IGNORECASE)
    KEY_PATTERNS = re.compile(r"(id|key|code|num|number|guid|uuid)$", re.IGNORECASE)
    NUMERIC_PATTERNS = re.compile(r"(amount|price|cost|revenue|qty|quantity|volume|rate|fee|tax|total|count)", re.IGNORECASE)

    @classmethod
    def profile_field(cls, field_name: str, declared_type: Optional[str] = None, sample_values: Optional[List[Any]] = None) -> ColumnProfile:
        name = str(field_name or "").strip()
        decl = str(declared_type or "").upper()

        is_date = bool(cls.DATE_PATTERNS.search(name)) or any(d in decl for d in ["DATE", "TIME", "TIMESTAMP"])
        is_key = bool(cls.KEY_PATTERNS.search(name))
        is_num = bool(cls.NUMERIC_PATTERNS.search(name)) or any(n in decl for n in ["INT", "NUM", "DEC", "FLOAT", "DOUBLE", "MONEY"])

        if is_date:
            inferred = DataType.DATETIME
        elif is_num and not (is_key and "INT" not in decl):
            inferred = DataType.DOUBLE if any(f in decl for f in ["DEC", "FLOAT", "DOUBLE", "REAL"]) or not is_key else DataType.INTEGER
        elif "BOOL" in decl:
            inferred = DataType.BOOLEAN
        else:
            inferred = DataType.STRING

        return ColumnProfile(
            column_name=name,
            inferred_type=inferred,
            is_candidate_key=is_key,
            is_foreign_key_candidate=is_key,
            is_numeric=is_num,
            is_date=is_date,
        )

    @classmethod
    def profile_table(cls, table: Dict[str, Any]) -> UniversalSourceTable:
        t_name = table.get("name") or table.get("table_name") or "Table"
        raw_cols = table.get("columns") or table.get("fields") or []
        fields = []

        for c in raw_cols:
            if isinstance(c, str):
                prof = cls.profile_field(c)
                fields.append(UniversalSourceField(
                    field_id=f"{t_name}.{c}",
                    name=c,
                    data_type=prof.inferred_type,
                    table_name=t_name,
                    is_key=prof.is_candidate_key,
                ))
            elif isinstance(c, dict):
                c_name = c.get("fabric_column_name") or c.get("qlik_column_name") or c.get("name") or "col"
                decl_t = c.get("fabric_datatype") or c.get("qlik_datatype") or c.get("dataType")
                prof = cls.profile_field(c_name, decl_t)
                fields.append(UniversalSourceField(
                    field_id=f"{t_name}.{c_name}",
                    name=c_name,
                    data_type=prof.inferred_type,
                    table_name=t_name,
                    is_key=prof.is_candidate_key,
                    format_string=c.get("format_string"),
                ))

        return UniversalSourceTable(
            table_id=t_name,
            name=t_name,
            connection_details=table.get("connection") or table.get("connection_details") or {},
            custom_sql=table.get("custom_sql"),
            upstream_table=table.get("upstream_table"),
            fields=fields,
            is_mapping_table=bool(table.get("is_mapping")),
        )
