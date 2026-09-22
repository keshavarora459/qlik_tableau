from typing import Any

from pydantic import BaseModel, Field


class CosmosLogPayload(BaseModel):
    agent_name: str
    project_name: str
    project_id: str
    workbook_id: str
    run_id: str
    log_level: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: str

class CosmosActivityPayload(BaseModel):
    run_id: str
    project_id: str
    workbook_id: str
    agent_name: str
    activity_summary: str
    status: str
    timestamp: str

class CosmosErrorPayload(BaseModel):
    run_id: str
    project_id: str
    workbook_id: str
    agent_name: str
    error_summary: str
    technical_details: str
    suggested_fix: str
    status: str
    timestamp: str

class CosmosMappingPayload(BaseModel):
    run_id: str
    project_id: str
    project_name: str
    workbook_id: str
    status: str
    created_at: str
    payload: dict[str, Any]
