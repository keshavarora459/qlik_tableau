# Upgraded Tableau parsing_service to fetch all parsing results reliably
import os
from typing import Any, Dict, List, Optional
import requests
from fastapi import HTTPException

from app.core.config import settings
from app.core.resilient_http import resilient_get
from app.core.structured_logger import get_structured_logger
from app.services.cosmos_log_service import store_activity, store_error, store_log

logger = get_structured_logger(__name__)


def _extract_payload_from_doc(doc: dict) -> dict:
    """Extracts or reconstructs the parsing payload dictionary from the raw database document."""
    if not isinstance(doc, dict):
        return {}

    # Check for direct payload or parsing_result envelope
    payload = doc.get("payload") or doc.get("parsing_result")
    if isinstance(payload, dict) and payload:
        # Carry over identifiers from top-level if missing inside payload
        for id_field in ["project_id", "workbook_id", "run_id", "project_name"]:
            if not payload.get(id_field) and doc.get(id_field):
                payload[id_field] = doc[id_field]
        return payload

    # If parsing sections exist at root level
    recognized_keys = {
        "metadata", "datasources", "logical_model", "relationships",
        "calculated_fields", "parameters", "sheets", "dashboards",
        "formatting", "security", "extracted_files"
    }
    if any(k in doc for k in recognized_keys):
        return doc

    return {}


def fetch_parsing_result(
    project_id: Optional[str] = "",
    workbook_id: Optional[str] = "",
    run_id: Optional[str] = "",
    auth_header: Optional[str] = "",
    direct_payload: Optional[dict] = None
) -> dict:
    """
    Fetch parsing results reliably using:
    1. Direct payload passed in request (if present)
    2. MongoDB search across parsing_results & parsing collections with multi-field fallback
    3. HTTP API fallback
    """
    # ── 1. Direct Payload ──────────────────────────────────────────
    if direct_payload and isinstance(direct_payload, dict):
        payload = _extract_payload_from_doc(direct_payload)
        if payload:
            logger.info("Using direct parsing payload provided in request.")
            return {
                "project_id": project_id or payload.get("project_id", ""),
                "workbook_id": workbook_id or payload.get("workbook_id", ""),
                "run_id": run_id or payload.get("run_id", ""),
                "project_name": payload.get("project_name", "Unknown"),
                "payload": payload,
            }

    store_activity(
        project_id=project_id or "unknown",
        workbook_id=workbook_id or "unknown",
        run_id=run_id or "unknown",
        technical_message="Fetching raw JSON parsing results from MongoDB/storage.",
        auth_header=auth_header or ""
    )

    parsing_response = None
    mongo_uri = settings.mongo_uri or os.getenv("MONGO_URI")

    # ── 2. MongoDB Query ───────────────────────────────────────────
    if mongo_uri:
        try:
            import pymongo
            client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=4000)

            # Try primary Tableau DB first, then shared DB
            db_names_to_try = [
                settings.mongo_db_name or "QT2F_Tableau",
                "QT2F",
            ]
            collections_to_try = ["parsing_results", "parsing"]

            # Queries ordered from most specific to most general
            queries: List[Dict[str, Any]] = []
            if project_id and workbook_id and run_id:
                queries.append({
                    "project_id": project_id,
                    "workbook_id": workbook_id,
                    "run_id": run_id
                })

            if run_id:
                queries.append({"run_id": run_id})
                queries.append({"run_no": run_id})
                queries.append({"id": run_id})

            if workbook_id:
                queries.append({"workbook_id": workbook_id})
                queries.append({"id": workbook_id})

            if run_id and workbook_id:
                queries.append({"$or": [{"run_id": run_id}, {"workbook_id": workbook_id}]})

            for db_name in db_names_to_try:
                if parsing_response:
                    break
                db = client[db_name]
                for coll_name in collections_to_try:
                    if parsing_response:
                        break
                    coll = db[coll_name]
                    for q in queries:
                        doc = coll.find_one(q, sort=[("_id", -1)])
                        if doc:
                            payload_extracted = _extract_payload_from_doc(doc)
                            if payload_extracted:
                                parsing_response = doc
                                parsing_response["payload"] = payload_extracted
                                logger.info(
                                    f"Successfully found parsing results in {db_name}.{coll_name} "
                                    f"using query: {q}"
                                )
                                break

        except Exception as exc:
            logger.warning(f"MongoDB query failed: {exc}, attempting fallback...", exc_info=True)

    # ── 3. HTTP API Fallback ───────────────────────────────────────
    if not parsing_response:
        api_bases = []
        for base in [
            settings.parsing_results_base_url,
            settings.cosmosdb_api_url,
            os.getenv("COSMOS_BASE_API"),
            os.getenv("BASE_API_URL"),
        ]:
            if base and base.strip() and base.strip() not in api_bases:
                api_bases.append(base.strip().rstrip("/"))

        headers = {"Authorization": auth_header} if auth_header else {}

        urls_to_try = []
        for b in api_bases:
            if run_id:
                urls_to_try.append(f"{b}/api/records/parsing/by-run/{run_id}")
                urls_to_try.append(f"{b}/parsing/by-run/{run_id}")
                urls_to_try.append(f"{b}/api/records/parsing/{run_id}")
            if workbook_id:
                urls_to_try.append(f"{b}/api/records/parsing/by-workbook/{workbook_id}")
                urls_to_try.append(f"{b}/parsing/by-app/{workbook_id}")

        for url in urls_to_try:
            try:
                resp = requests.get(url, headers=headers, timeout=5)
                if resp.status_code == 200:
                    api_data = resp.json()
                    target = None
                    if isinstance(api_data, list) and api_data:
                        target = api_data[0]
                    elif isinstance(api_data, dict):
                        target = api_data

                    if target:
                        payload_extracted = _extract_payload_from_doc(target)
                        if payload_extracted:
                            parsing_response = target
                            parsing_response["payload"] = payload_extracted
                            logger.info(f"Successfully fetched parsing results via HTTP from: {url}")
                            break
            except Exception as e:
                logger.debug(f"HTTP fetch failed for {url}: {e}")

    # ── 4. Validate and Return ─────────────────────────────────────
    if not parsing_response or not parsing_response.get("payload"):
        store_error(
            project_id=project_id or "unknown",
            workbook_id=workbook_id or "unknown",
            run_id=run_id or "unknown",
            error_msg="No parsing results found for given project/workbook/run.",
            technical_details=f"Searched MongoDB and HTTP fallback for run_id={run_id}, workbook_id={workbook_id}, project_id={project_id}.",
            auth_header=auth_header or ""
        )
        raise HTTPException(
            status_code=404,
            detail="No parsing results found for given project/workbook/run"
        )

    if "_id" in parsing_response:
        parsing_response["_id"] = str(parsing_response["_id"])

    # Backfill top-level identifiers if missing
    resolved_proj_id = parsing_response.get("project_id") or project_id or ""
    resolved_wb_id = parsing_response.get("workbook_id") or workbook_id or ""
    resolved_run_id = parsing_response.get("run_id") or run_id or ""
    resolved_proj_name = parsing_response.get("project_name") or "Unknown"

    parsing_response["project_id"] = resolved_proj_id
    parsing_response["workbook_id"] = resolved_wb_id
    parsing_response["run_id"] = resolved_run_id
    parsing_response["project_name"] = resolved_proj_name

    store_log(
        project_id=resolved_proj_id,
        project_name=resolved_proj_name,
        workbook_id=resolved_wb_id,
        run_id=resolved_run_id,
        function_name="fetch_parsing_result",
        log_level="INFO",
        message="Successfully fetched and validated parsing payload.",
        auth_header=auth_header or ""
    )

    return parsing_response
