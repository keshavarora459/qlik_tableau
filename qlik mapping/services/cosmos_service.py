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
            coll = db["parsing"]

            query = {}
            if run_id and app_id:
                query = {"$or": [{"run_id": run_id}, {"app_id": app_id}]}
            elif run_id:
                query = {"run_id": run_id}
            elif app_id:
                query = {"app_id": app_id}

            doc = coll.find_one(query, sort=[("_id", -1)])
            if doc:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
                logger.info(f"Successfully fetched parsing directly from MongoDB for run_id={run_id}, app_id={app_id}")
                pr = doc.get("parsing_result")
                if isinstance(pr, dict) and pr:
                    return _enrich_parsing_result(pr, doc)
                return _enrich_parsing_result(doc, doc)
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
                                pr = target.get("parsing_result")
                                if isinstance(pr, dict) and pr:
                                    return _enrich_parsing_result(pr, target)
                                return _enrich_parsing_result(target, target)
                        elif isinstance(data, dict) and not _is_not_found_body(data):
                            pr = data.get("parsing_result")
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
            coll = db["mapping"]

            query = {}
            if run_id and app_id:
                query = {"$or": [{"run_id": run_id}, {"app_id": app_id}]}
            elif run_id:
                query = {"run_id": run_id}
            elif app_id:
                query = {"app_id": app_id}

            doc = coll.find_one(query, sort=[("_id", -1)])
            if doc:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
                logger.info(f"Successfully fetched mapping directly from MongoDB for run_id={run_id}, app_id={app_id}")
                mr = doc.get("mapping_result")
                if isinstance(mr, dict) and mr:
                    return mr
                return doc
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
                                mapping_result = target.get("mapping_result")
                                if isinstance(mapping_result, dict) and mapping_result:
                                    return mapping_result
                                return target
                        elif isinstance(data, dict) and not _is_not_found_body(data):
                            return data.get("mapping_result") or data
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
    if not app_id and not run_id:
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
            coll = db["mapping"]

            # Save all MAPPING results in a subfolder (nested object) and rest outside
            doc_to_save = {
                "mapping_result": mapping_result if isinstance(mapping_result, dict) else {}
            }
            
            _m_res = doc_to_save["mapping_result"]
            _orig_req = _m_res.get("original_request_payload", {})
            
            doc_to_save["app_id"] = app_id or _m_res.get("app_id", "") or _orig_req.get("app_id", "")
            doc_to_save["space_id"] = space_id or _m_res.get("space_id", "") or _orig_req.get("space_id", "")
            doc_to_save["app_name"] = app_name or _m_res.get("app_name", "") or _orig_req.get("app_name", "")
            doc_to_save["run_id"] = run_id or _m_res.get("run_id", "") or _orig_req.get("run_id", "")
            doc_to_save["workspace_id"] = _m_res.get("workspace_id", "") or _orig_req.get("workspace_id", "")
            doc_to_save["folder_name"] = _m_res.get("folder_name", "") or _orig_req.get("folder_name", "")
            doc_to_save["updated_at"] = datetime.datetime.utcnow().isoformat()

            query = {}
            if run_id:
                query = {"run_id": run_id}
            elif app_id:
                query = {"app_id": app_id}

            if query:
                coll.replace_one(query, doc_to_save, upsert=True)
            else:
                coll.insert_one(doc_to_save)

            logger.info(f"Successfully saved mapping directly to MongoDB collection 'mapping' for run_id={run_id}, app_id={app_id}")
            saved_directly = True
        except Exception as me:
            logger.warning(f"Direct MongoDB save failed: {me}")

    # ── 2. HTTP API fallback / sync ─────
    last_error = None
    for base in _get_base_apis():
        try:
            url = f"{base}/mapping"
            payload = {}
            if isinstance(mapping_result, dict):
                payload.update(mapping_result)
            
            payload["app_id"] = app_id or payload.get("app_id", "")
            payload["space_id"] = space_id or payload.get("space_id", "")
            payload["app_name"] = app_name or payload.get("app_name", "")
            payload["run_id"] = run_id or payload.get("run_id", "")
            token = request_token_ctx.get()
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = f"Bearer {token}"
                
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(url, json=payload, timeout=30) as response:
                    if response.status in (200, 201):
                        return {"status": "success", "message": "Mapping result saved"}
        except Exception as e:
            last_error = e
            logger.warning(f"HTTP save mapping to {base} warning: {e}")

    if saved_directly:
        return {"status": "success", "message": "Mapping result saved directly to MongoDB"}

    return {"status": "error", "message": str(last_error)}

