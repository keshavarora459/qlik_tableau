import os
import sys
from pathlib import Path

# Ensure root directory containing 'common' is in sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import uuid
import logging
from fastapi import FastAPI, HTTPException, Request
from models import MappingRequest
from auth import validate_request_bearer_token
from services.api_logger import log_action_to_api, log_error_to_api
from services.cosmos_service import (
    fetch_parsing_from_cosmos,
    fetch_mapping_from_cosmos,
    save_mapping_to_cosmos,
)
from agents import CoordinatorAgent
from logging_config import logger
from config import Config
Config.validate()

app = FastAPI(title="Mapping Agent API", version="2.0.0")

@app.get("/")
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "qlik-mapping-agent",
        "version": "2.0.0"
    }

@app.post("/api/mapping")
async def post_mapping_data(payload: MappingRequest, request: Request):
    """API endpoint to process Qlik parsing data from MongoDB API into Fabric/Power BI mapping structures."""
    logger.info("Received POST request for /api/mapping")

    try:
        try:
            user_payload = await validate_request_bearer_token(request)
            logger.info(f"Authenticated user: {user_payload.get('sub', 'unknown')}")
        except Exception as he:
            logger.warning(f"Auth header validation bypassed for mapping request: {he}")
            user_payload = {"sub": "system_user"}

        req_app_id = payload.app_id
        req_run_id = payload.run_id

        if not req_app_id and not req_run_id:
            error_msg = "app_id or run_id is mandatory in the payload"
            await log_error_to_api(error_msg, app_id=req_app_id, endpoint="/api/mapping", status_code=400)
            raise HTTPException(status_code=400, detail={"status": "error", "message": error_msg})

        is_direct_query = payload.is_direct_query
        force_refresh = payload.force_refresh

        if not force_refresh:
            cached_mapping = await fetch_mapping_from_cosmos(req_app_id, req_run_id)
            if cached_mapping is not None and cached_mapping.get("tables"):
                from services.connection_mapper import ConnectionMapper
                from src.converters.dashboard_objects.converter import DashboardObjectConverter
                from src.converters.base import ConversionContext
                mapper = ConnectionMapper()
                dashboard_converter = DashboardObjectConverter()
                ctx = ConversionContext(app_id=req_app_id, tables=cached_mapping.get("tables", []))
                for table in cached_mapping.get("tables", []):
                    # Check top-level table dict
                    if "mquery" in table and isinstance(table["mquery"], str):
                        table["m_query"] = mapper.parse_mquery_to_steps(table["mquery"])
                        del table["mquery"]
                    elif "m_query" in table and isinstance(table["m_query"], str):
                        table["m_query"] = mapper.parse_mquery_to_steps(table["m_query"])
                    
                    # Extract m_query from fabric partition if it isn't at the top level
                    if "fabric" in table and isinstance(table["fabric"], dict):
                        if "partition" in table["fabric"] and isinstance(table["fabric"]["partition"], dict):
                            if "m_expression" in table["fabric"]["partition"] and isinstance(table["fabric"]["partition"]["m_expression"], str):
                                if "m_query" not in table or not isinstance(table["m_query"], list):
                                    table["m_query"] = mapper.parse_mquery_to_steps(table["fabric"]["partition"]["m_expression"])
                        # Delete the fabric block as requested
                        del table["fabric"]
                
                # Re-apply DashboardObjectConverter on cached visuals so latest translations are always returned
                if "visuals" in cached_mapping and isinstance(cached_mapping["visuals"], dict):
                    sheet_visuals = cached_mapping["visuals"].get("sheet_visuals", [])
                    raw_visuals = [v.get("qlik_source") or v for v in sheet_visuals if isinstance(v, dict)]
                    raw_sheets = cached_mapping["visuals"].get("sheets", [])
                    
                    if raw_visuals:
                        new_sheet_visuals = []
                        sheet_map = {}
                        import uuid
                        
                        for v in raw_visuals:
                            converted_item = await dashboard_converter.convert_one(v, ctx)
                            v_item = {
                                "qlik_source": converted_item.source,
                                "fabric": converted_item.fabric,
                                "confidence": converted_item.confidence
                            }
                            sheet_title = v.get("sheet_name") or v.get("source") or "Main Sheet"
                            new_sheet_visuals.append(v_item)
                            sheet_map.setdefault(sheet_title, []).append(v_item)
                            
                        sheets_list = []
                        if raw_sheets:
                            for s in raw_sheets:
                                stitle = s.get("title") or s.get("name") or "Sheet"
                                s_vis = sheet_map.get(stitle, new_sheet_visuals if len(raw_sheets) == 1 else [])
                                sheets_list.append({
                                    "sheet_id": s.get("sheet_id") or str(uuid.uuid4()),
                                    "title": stitle,
                                    "visualization_count": len(s_vis),
                                    "visualizations": s_vis
                                })
                        else:
                            for stitle, s_vis in sheet_map.items():
                                sheets_list.append({
                                    "sheet_id": str(uuid.uuid4()),
                                    "title": stitle,
                                    "visualization_count": len(s_vis),
                                    "visualizations": s_vis
                                })
                                
                        cached_mapping["visuals"] = {
                            "sheet_visuals": new_sheet_visuals,
                            "sheets": sheets_list
                        }
                        
                cached_mapping["source"] = "cosmos_cache"
                cache_app_id = req_app_id or cached_mapping.get("app_id", "")
                await log_action_to_api(
                    "Returned cached mapping from Cosmos", app_id=cache_app_id, run_id=req_run_id,
                    details=f"{len(cached_mapping.get('tables', []))} table(s), "
                            f"{len(cached_mapping.get('visuals', {}).get('sheet_visuals', []))} visual(s) re-served from cache",
                )
                return cached_mapping

        # Check if direct parsing data was provided in request payload (e.g. Postman direct test)
        payload_dict = payload.model_dump()
        if payload.tables or payload.measures or payload.visualizations:
            data = payload_dict
        else:
            # Fetch parsing result directly from MongoDB API by run_id or app_id
            data = await fetch_parsing_from_cosmos(req_app_id, req_run_id)

        if not data:
            error_msg = f"No parsing data found in MongoDB for run_id: {req_run_id} or app_id: {req_app_id}"
            await log_error_to_api(error_msg, app_id=req_app_id, endpoint="/api/mapping", status_code=400)
            raise HTTPException(status_code=400, detail={"status": "error", "message": error_msg})

        app_id = req_app_id or data.get("app_id", "")
        space_id = payload.space_id or data.get("space_id", "")
        app_name = payload.app_name or data.get("app_name", "")
        run_id = req_run_id or data.get("run_id", str(uuid.uuid4()))

        coordinator = CoordinatorAgent()
        result = await coordinator.process_data(data, is_direct_query, app_id, space_id, app_name, run_id)
        result["source"] = "fresh"
        result["original_request_payload"] = payload_dict

        if isinstance(result.get("workbook_metadata"), dict):
            # process_data already resolves these the same way (payload wins
            # over raw parsing data); reasserted here so a downstream mutation
            # of `result` before this point can never leave them stale.
            result["workbook_metadata"]["app_id"] = app_id
            result["workbook_metadata"]["space_id"] = space_id
            result["workbook_metadata"]["app_name"] = app_name
            result["workbook_metadata"]["name"] = app_name
            result["workbook_metadata"]["run_id"] = run_id

        save_result = await save_mapping_to_cosmos(app_id, space_id, app_name, run_id, result)
        await log_action_to_api(
            "Completed mapping process successfully", app_id=app_id, run_id=run_id,
            details=f"app_name='{app_name}', space_id='{space_id}', mongo_save_status='{save_result.get('status')}'",
        )
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in POST endpoint: {str(e)}")
        await log_error_to_api(str(e), app_id=payload.app_id, endpoint="/api/mapping", status_code=500)
        raise HTTPException(status_code=500, detail={"status": "error", "message": f"Unexpected error: {str(e)}"})

if __name__ == '__main__':
    from config import Config
    Config.validate()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)