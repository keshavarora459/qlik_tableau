import logging
import aiohttp
from typing import Optional, Dict, Any
from config import COSMOS_DB_API
from auth import request_token_ctx

logger = logging.getLogger(__name__)


def _is_not_found_body(data: Dict[str, Any]) -> bool:
    """The MongoDB API answers a miss with HTTP 200 and a body like
    `{"message": "No record found for folder '...'"}` instead of a 404.
    Treating that as real data made /api/mapping silently return an empty
    'success' Contract 2.0 result for an app/run that was never parsed,
    instead of the 400 the caller needs to know nothing was found."""
    return isinstance(data, dict) and "message" in data and not (
        data.get("parsing_result") or data.get("mapping_result") or data.get("tables") or data.get("fields")
    )


import os

def _get_base_apis() -> list:
    """Collect configured MongoDB API endpoints from environment variables."""
    apis = []
    for env_var in ["BASE_API_URL", "AGENT_ACTIONS_API_URL", "COSMOS_BASE_API", "MONGO_API_URL", "QLIK_MONGO_API_URL", "COSMOS_DB_API"]:
        val = os.getenv(env_var)
        if val and val.strip():
            clean_val = val.strip().rstrip("/")
            if clean_val not in apis:
                apis.append(clean_val)
    if not apis and COSMOS_DB_API:
        apis.append(COSMOS_DB_API)
    return apis


import asyncio

def _enrich_parsing_result(pr: Dict[str, Any], envelope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Ensure the parsing_result dict carries summary and _meta from its envelope or _meta block."""
    if not isinstance(pr, dict):
        return pr

    env = envelope if isinstance(envelope, dict) else {}

    # 1. Preserve summary
    if "summary" not in pr or not pr["summary"]:
        summary = env.get("summary") or (pr.get("_meta", {}).get("summary") if isinstance(pr.get("_meta"), dict) else None) or (env.get("_meta", {}).get("summary") if isinstance(env.get("_meta"), dict) else None)
        if summary and isinstance(summary, dict):
            pr["summary"] = summary

    # 2. Preserve _meta
    if "_meta" not in pr or not pr["_meta"]:
        meta = env.get("_meta")
        if meta and isinstance(meta, dict):
            pr["_meta"] = meta

    return pr


async def fetch_parsing_from_cosmos(app_id: str, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch parsed metadata directly from MongoDB first, then HTTP API fallback."""

    # ── 1. Direct MongoDB (fastest & most reliable) ─────
    mongo_uri = os.getenv("MONGO_URI") or os.getenv("COSMOS_CONNECTION_STRING") or os.getenv("MONGODB_URI")
    if mongo_uri:
        try:
            from pymongo import MongoClient
            db_name = os.getenv("QLIK_MONGO_DB_NAME") or os.getenv("MONGO_DB_NAME", "QT2F")
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=4000)
            db = client[db_name]

            doc = None
            # Search parsing, parsing_results, and assessment collections
            for coll_name in ["parsing", "parsing_results", "assessment_results", "assessment"]:
                if coll_name in db.list_collection_names():
                    coll = db[coll_name]
                    # First try exact run_id
                    if run_id:
                        doc = coll.find_one({"$or": [{"run_id": run_id}, {"id": run_id}]}, sort=[("_id", -1)])
                    # Then try app_id / workbook_id
                    if not doc and app_id:
                        doc = coll.find_one(
                            {"$or": [{"app_id": app_id}, {"workbook_id": app_id}, {"folder_name": app_id}, {"id": app_id}]},
                            sort=[("_id", -1)]
                        )
                    if doc:
                        logger.info(f"Found metadata record in MongoDB '{db_name}.{coll_name}' for run_id={run_id}, app_id={app_id}")
                        break

            if doc:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
                pr = doc.get("parsing_result") or doc.get("payload") or doc.get("assessment_result")
                if isinstance(pr, dict) and pr:
                    return _enrich_parsing_result(pr, doc)
                return _enrich_parsing_result(doc, doc)
            else:
                logger.info(f"No parsing/assessment record in MongoDB '{db_name}' for run_id={run_id}, app_id={app_id}, trying HTTP API fallback...")
        except Exception as me:
            logger.warning(f"Direct MongoDB fetch failed: {me}, trying HTTP API fallback...")

    # ── 2. HTTP API fallback ─────
    token = request_token_ctx.get()
    headers = {'Authorization': f"Bearer {token}"} if token else {}
    timeout = aiohttp.ClientTimeout(total=30)

    urls_to_try = []
    for base in _get_base_apis():
        if run_id:
            urls_to_try.append(f"{base}/parsing/by-run/{run_id}")
        if app_id:
            urls_to_try.append(f"{base}/parsing/by-app/{app_id}")

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for url in urls_to_try:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list) and data:
                            target = None
                            for item in data:
                                if isinstance(item, dict):
                                    if (run_id and item.get("run_id") == run_id) or (app_id and item.get("app_id") == app_id):
                                        target = item
                                        break
                            if not target and data:
                                target = data[0] if isinstance(data[0], dict) else None

                            if target:
                                pr = target.get("parsing_result") or target.get("payload") or target.get("assessment_result")
                                if isinstance(pr, dict) and pr:
                                    return _enrich_parsing_result(pr, target)
                                return _enrich_parsing_result(target, target)
                        elif isinstance(data, dict) and not _is_not_found_body(data):
                            pr = data.get("parsing_result") or data.get("payload") or data.get("assessment_result")
                            if isinstance(pr, dict) and pr:
                                return _enrich_parsing_result(pr, data)
                            return _enrich_parsing_result(data, data)
            except Exception as e:
                logger.warning(f"Failed to fetch parsing from {url}: {e}")

    return None


async def fetch_mapping_from_cosmos(app_id: str, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch mapped result via direct MongoDB first, then HTTP API fallback."""

    # ── 1. Direct MongoDB (fastest) ─────
    mongo_uri = os.getenv("MONGO_URI") or os.getenv("COSMOS_CONNECTION_STRING") or os.getenv("MONGODB_URI")
    if mongo_uri:
        try:
            from pymongo import MongoClient
            db_name = os.getenv("QLIK_MONGO_DB_NAME") or os.getenv("MONGO_DB_NAME", "QT2F")
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=4000)
            db = client[db_name]

            doc = None
            for coll_name in ["mapping", "mapping_results"]:
                if coll_name in db.list_collection_names():
                    coll = db[coll_name]
                    if run_id:
                        doc = coll.find_one({"$or": [{"run_id": run_id}, {"id": run_id}]}, sort=[("_id", -1)])
                    if not doc and app_id:
                        doc = coll.find_one(
                            {"$or": [{"app_id": app_id}, {"workbook_id": app_id}, {"folder_name": app_id}, {"id": app_id}]},
                            sort=[("_id", -1)]
                        )
                    if doc:
                        break

            if doc:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
                logger.info(f"Successfully fetched mapping directly from MongoDB for run_id={run_id}, app_id={app_id}")
                mr = doc.get("mapping_result") or doc.get("payload")
                if isinstance(mr, dict) and mr:
                    return mr
                return doc
            else:
                logger.info(f"No mapping record in MongoDB '{db_name}' for run_id={run_id}, app_id={app_id}, trying HTTP API fallback...")
        except Exception as me:
            logger.warning(f"Direct MongoDB fetch for mapping failed: {me}, trying HTTP API fallback...")

    # ── 2. HTTP API fallback ─────
    token = request_token_ctx.get()
    headers = {'Authorization': f"Bearer {token}"} if token else {}
    timeout = aiohttp.ClientTimeout(total=30)

    urls_to_try = []
    for base in _get_base_apis():
        if run_id:
            urls_to_try.append(f"{base}/mapping/by-run/{run_id}")
        if app_id:
            urls_to_try.append(f"{base}/mapping/by-app/{app_id}")

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for url in urls_to_try:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list) and data:
                            target = None
                            for item in data:
                                if isinstance(item, dict):
                                    if (run_id and item.get("run_id") == run_id) or (app_id and item.get("app_id") == app_id):
                                        target = item
                                        break
                            if not target and data:
                                target = data[0] if isinstance(data[0], dict) else None

                            if target:
                                mapping_result = target.get("mapping_result") or target.get("payload")
                                if isinstance(mapping_result, dict) and mapping_result:
                                    return mapping_result
                                return target
                        elif isinstance(data, dict) and not _is_not_found_body(data):
                            return data.get("mapping_result") or data.get("payload") or data
            except Exception as e:
                logger.warning(f"Failed to fetch mapping from {url}: {e}")

    return None


async def save_mapping_to_cosmos(
    app_id: str, 
    space_id: str, 
    app_name: str, 
    run_id: str, 
    mapping_result: Dict[str, Any]
) -> Dict[str, str]:
    """Save converted mapping result back to MongoDB directly and via HTTP API."""
    _m_res = mapping_result if isinstance(mapping_result, dict) else {}
    _orig_req = _m_res.get("original_request_payload", {})
    _wm = _m_res.get("workbook_metadata", {}) if isinstance(_m_res.get("workbook_metadata"), dict) else {}

    resolved_app_id = (
        app_id 
        or _wm.get("app_id") 
        or _wm.get("workbook_id") 
        or _m_res.get("app_id") 
        or _orig_req.get("app_id") 
        or ""
    )
    resolved_run_id = (
        run_id 
        or _wm.get("run_id") 
        or _m_res.get("run_id") 
        or _orig_req.get("run_id") 
        or ""
    )
    resolved_app_name = (
        app_name 
        or _wm.get("app_name") 
        or _wm.get("name") 
        or _m_res.get("app_name") 
        or _orig_req.get("app_name") 
        or ""
    )
    resolved_space_id = (
        space_id 
        or _wm.get("space_id") 
        or _m_res.get("space_id") 
        or _orig_req.get("space_id") 
        or ""
    )
    workspace_id = (
        _wm.get("workspace_id") 
        or _m_res.get("workspace_id") 
        or _orig_req.get("workspace_id") 
        or "personal"
    )

    if not resolved_app_id and not resolved_run_id:
        return {"status": "skipped", "message": "No app_id or run_id provided"}

    saved_directly = False

    # ── 1. Direct MongoDB save (fastest & most reliable) ─────
    mongo_uri = os.getenv("MONGO_URI") or os.getenv("COSMOS_CONNECTION_STRING") or os.getenv("MONGODB_URI")
    if mongo_uri:
        try:
            from pymongo import MongoClient
            import datetime
            db_name = os.getenv("QLIK_MONGO_DB_NAME") or os.getenv("MONGO_DB_NAME", "QT2F")
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=4000)
            db = client[db_name]

            doc_to_save = {
                "mapping_result": _m_res,
                "app_id": resolved_app_id,
                "workbook_id": resolved_app_id,
                "space_id": resolved_space_id,
                "app_name": resolved_app_name,
                "run_id": resolved_run_id,
                "workspace_id": workspace_id,
                "folder_name": resolved_app_id or resolved_app_name,
                "updated_at": datetime.datetime.utcnow().isoformat()
            }

            query = {}
            if resolved_run_id:
                query = {"run_id": resolved_run_id}
            elif resolved_app_id:
                query = {"app_id": resolved_app_id}

            # Upsert into both 'mapping' and 'mapping_results' so any client or UI finds the data
            for col_name in ["mapping", "mapping_results"]:
                coll = db[col_name]
                if query:
                    coll.replace_one(query, doc_to_save, upsert=True)
                else:
                    coll.insert_one(doc_to_save)

            saved_directly = True
            logger.info(f"Successfully saved mapping directly to MongoDB collections 'mapping' and 'mapping_results' in '{db_name}' for run_id={resolved_run_id}, app_id={resolved_app_id}")
            return {"status": "success", "message": f"Mapping result saved directly to MongoDB '{db_name}.mapping' and '{db_name}.mapping_results'"}
        except Exception as me:
            logger.warning(f"Direct MongoDB save failed: {me}")

    # ── 2. HTTP API fallback / sync ─────
    last_error = None
    for base in _get_base_apis():
        try:
            url = f"{base}/mapping"
            _m_res = mapping_result if isinstance(mapping_result, dict) else {}
            _orig_req = _m_res.get("original_request_payload", {})
            
            payload = {
                "app_id": app_id or _m_res.get("app_id", "") or _orig_req.get("app_id", ""),
                "space_id": space_id or _m_res.get("space_id", "") or _orig_req.get("space_id", ""),
                "app_name": app_name or _m_res.get("app_name", "") or _orig_req.get("app_name", ""),
                "run_id": run_id or _m_res.get("run_id", "") or _orig_req.get("run_id", ""),
                "workspace_id": _m_res.get("workspace_id", "") or _orig_req.get("workspace_id", "") or "personal",
                "folder_name": app_id or "",
                "mapping_result": _m_res
            }
            token = request_token_ctx.get()
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = f"Bearer {token}"
                
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(url, json=payload, timeout=30) as response:
                    if response.status in (200, 201):
                        logger.info(f"Successfully saved mapping to AWS API {url} for run_id={run_id}")
                        return {"status": "success", "message": f"Mapping result saved to AWS API ({base})"}
        except Exception as e:
            last_error = e
            logger.warning(f"HTTP save mapping to {base} warning: {e}")

    if saved_directly:
        return {"status": "success", "message": "Mapping result saved directly to MongoDB"}

    return {"status": "error", "message": str(last_error)}

