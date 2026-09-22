from typing import Optional, Any, Dict, List
from pydantic import BaseModel, ConfigDict

class MappingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: Optional[str] = "run1"
    app_id: Optional[str] = None
    space_id: Optional[str] = None
    app_name: Optional[str] = None
    is_direct_query: bool = False
    force_refresh: bool = True
    tables: Optional[List[Dict[str, Any]]] = None
    measures: Optional[List[Dict[str, Any]]] = None
    dimensions: Optional[List[Dict[str, Any]]] = None
    visualizations: Optional[List[Dict[str, Any]]] = None
    relationships: Optional[List[Dict[str, Any]]] = None
