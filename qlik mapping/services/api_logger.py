import logging
from typing import Optional, Any
try:
    import aiohttp
except ImportError:
    aiohttp = None
from config import Config, HEADERS
from auth import request_token_ctx
from common.llm import redact_sensitive_data
from services.action_humanizer import action_humanizer, STAGE_FALLBACKS

logger = logging.getLogger(__name__)

async def log_action_to_api(
    action: str = None,
    action_desc: str = None,
    app_id: str = None,
    details: str = None,
    run_id: str = None,
    workspace_id: str = None,
    project_name: str = None,
    stage_key: Optional[str] = None
) -> None:
    """Log an agent action to the external monitoring API, optionally humanizing with LLM."""
    raw_action = action or action_desc or ""
    safe_action = str(redact_sensitive_data(raw_action))
    safe_details = str(redact_sensitive_data(details or (f"Processing app {app_id}" if app_id else "General action")))

    # Use fast instant stage templates to prevent LLM latency on logs
    humanized = STAGE_FALLBACKS.get(stage_key, {
        "action": safe_action,
        "summary": safe_details
    })
    safe_action = humanized.get("action", safe_action)
    safe_details = humanized.get("summary", safe_details)

    logger.info(f"Agent Action: '{safe_action}', app_id='{app_id}', run_id='{run_id}', details='{safe_details}'")

    base_api = (Config.AGENT_ACTIONS_API_URL or Config.BASE_API_URL or "").rstrip('/')
    if not base_api:
        return
    url = f"{base_api}/agent-actions"
    data = {
        "agent_name": "Mapping Agent",
        "activity_summary": safe_details,
        "action": safe_action,
        "details": safe_details,
        "correlation_id": run_id or app_id or "unknown",
        "run_id": run_id or "",
        "run_no": run_id or "",
        "app_id": app_id or "",
        "workbook_id": app_id or "",
        "workspace_id": workspace_id or "personal",
        "project_id": workspace_id or "personal",
        "project_name": project_name or "Unknown",
        "type": "agent_activity",
        "status": "success",
    }
    token = request_token_ctx.get()
    headers = HEADERS.copy()
    if token:
        headers['Authorization'] = f"Bearer {token}"
        
    async def _send_log_async():
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=1.5)) as response:
                    pass
        except Exception:
            pass

    # Fire background task so network log calls NEVER block API responses
    import asyncio
    try:
        asyncio.create_task(_send_log_async())
    except Exception:
        pass


async def log_agent_log_to_api(
    message: str,
    agent_name: str = "Mapping Agent",
    log_level: str = "INFO",
    function_name: str = None,
    details: Any = None,
    run_id: str = None,
    workspace_id: str = None,
    app_id: str = None,
    correlation_id: str = None
) -> None:
    """Log an internal agent execution log to the external MongoDB agent_logs collection."""
    safe_msg = str(redact_sensitive_data(message))
    base_api = (Config.BASE_API_URL or "").rstrip('/')
    if not base_api:
        return
    url = f"{base_api}/agent-logs"
    data = {
        "agent_name": agent_name,
        "log_level": (log_level or "INFO").upper(),
        "message": safe_msg,
        "details": details if isinstance(details, (dict, list, str, int, float, bool)) else (str(details) if details else None),
        "function_name": function_name or "process_data",
        "correlation_id": correlation_id or run_id or app_id or "unknown",
        "run_id": run_id or "",
        "workspace_id": workspace_id or "",
        "app_id": app_id or "",
    }
    token = request_token_ctx.get()
    headers = HEADERS.copy()
    if token:
        headers['Authorization'] = f"Bearer {token}"

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=2.0)) as response:
                if response.status not in [200, 201]:
                    logger.debug(f"Agent log response status={response.status} from {url}")
        except Exception as e:
            logger.debug(f"API agent log unavailable: {redact_sensitive_data(str(e)[:100])}")


async def log_error_to_api(
    error_message: str, 
    app_id: str = None, 
    endpoint: str = None, 
    status_code: int = None
) -> None:
    """Log an API error to the external monitoring API."""
    base_api = (Config.BASE_API_URL or "").rstrip('/')
    if not base_api:
        return
    url = f"{base_api}/api-error-logs"
    safe_err = str(redact_sensitive_data(error_message))
    data = {
        "service_name": "Mapping",
        "endpoint": endpoint or "unknown",
        "status_code": status_code or 0,
        "error_message": safe_err[:150],
        "correlation_id": app_id if app_id else "unknown"
    }
    token = request_token_ctx.get()
    headers = HEADERS.copy()
    if token:
        headers['Authorization'] = f"Bearer {token}"
        
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=1.5)) as response:
                if response.status not in [200, 201]:
                    logger.debug(f"Error logger response status={response.status} from {url}")
        except Exception as e:  # noqa: BLE001 - a monitoring-endpoint hiccup must never break the mapping run
            logger.debug(f"API log error unavailable: {str(e)[:100]}")
