from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict


class MeasureRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    project_id: Optional[str] = ""
    workbook_id: Optional[str] = ""
    skip_llm: bool = False
    payload: Optional[Dict[str, Any]] = None
    parsing_result: Optional[Dict[str, Any]] = None
