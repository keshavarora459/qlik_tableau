import asyncio
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.core.resilient_http import resilient_async_post
from app.core.structured_logger import get_structured_logger
from app.models.cosmos_contracts import CosmosActivityPayload, CosmosErrorPayload, CosmosLogPayload
from app.services.llm_service import call_llm

logger = get_structured_logger(__name__)

# 🔗 Centralized Log API
# Ideally, move this URL to settings.py
COSMOSDB_API_URL = settings.cosmosdb_api_url.rstrip("/")
LOG_API_URL = f'{COSMOSDB_API_URL}/agent-logs'
ACTIVITY_API_URL = f'{COSMOSDB_API_URL}/api/records/activities'
ERROR_API_URL = f'{COSMOSDB_API_URL}/api-error-logs'

# Throttle concurrent outbound Cosmos DB calls to prevent 429 rate-limit floods
# when 10+ workbooks run simultaneously (each sends dozens of log/activity calls)
_cosmos_api_semaphore = asyncio.Semaphore(5)
#

async def send_log_to_api(
    agent_name: str,
    project_name: str | None,
    project_id: str,
    workbook_id: str,
    run_id: str,
    log_level: str,
    message: str,
    auth_header: str,
    details: dict | None = None
):
    """
    Sends logs to COSMOSDB Agent Logs collection.

    Guarantees:
    - Fire-and-forget
    - Never raises
    - Never blocks parsing
    - Always JSON-safe
    """
    try:
        # --------------------------------------------------
        # 1. SAFETY: Normalize inputs & Build Payload
        # --------------------------------------------------
        # We wrap payload construction in try/catch to handle non-serializable data
        safe_project_name = project_name or "Unknown"

        # Ensure details is actually a dict, otherwise convert to string to prevent crashes
        safe_details = details if isinstance(details, dict) else {"raw": str(details)} if details else {}

        payload = CosmosLogPayload(
            agent_name=str(agent_name),
            project_name=str(safe_project_name),
            project_id=str(project_id),
            workbook_id=str(workbook_id),
            run_id=str(run_id),
            log_level=str(log_level),
            message=str(message),
            details=safe_details,
            timestamp=datetime.now(timezone.utc).isoformat()
        ).model_dump()

        # --------------------------------------------------
        # 2. EXECUTE: Async Request
        # --------------------------------------------------
        headers = {"Authorization": auth_header}
        response = await resilient_async_post(LOG_API_URL, json=payload, headers=headers)

        if response.status_code >= 400:
            logger.warning(f"Log API rejected log (status={response.status_code}): {response.text}")
            raise Exception(f"Log API rejected log (status={response.status_code}): {response.text}")
        return True

    except Exception as e:
        # We raise here so `cosmos_log_service`'s done-callback writes it to the dead-letter store
        logger.error(f"[FALLBACK LOG] {log_level}: {message} | (API Failed: {str(e)})")
        raise e

# Deduplication cache to prevent duplicate activity logs
seen_activities = set()

def clear_seen_activities():
    """Reset the deduplication cache at the start of a new API request."""
    global seen_activities
    seen_activities.clear()

async def send_activity_to_api(
    project_id: str,
    workbook_id: str,
    run_id: str,
    agent_name: str,
    technical_message: str,
    auth_header: str
):
    """
    Generates a summary using LLM and saves it to 'agent_activities', avoiding duplicates.
    """
    global seen_activities
    try:
        # Deduplication check (BEFORE LLM call to save time and ensure exact match)
        unique_key = f"{run_id}_{agent_name}_{technical_message}"
        if unique_key in seen_activities:
            logger.info(f"Skipping duplicate activity for: {technical_message}")
            return  # Skip sending to avoid duplicate logs

        seen_activities.add(unique_key)

        # 1. Generate Non-Technical Summary (Run sync LLM call in a background thread)
        system_prompt = "Generate a concise, unique action description (approximately 14-15 words) based on the input action."
        user_prompt = f"Action: {technical_message}"

        # Add fallback for LLM generation
        try:
            activity_summary = await asyncio.to_thread(call_llm, system_prompt, user_prompt)
            activity_summary = activity_summary.strip()
        except Exception as llm_error:
            logger.warning(f"LLM failed to generate activity summary, using fallback: {llm_error}")
            activity_summary = technical_message[:150] + "..." if len(technical_message) > 150 else technical_message

        # 2. Prepare Payload
        payload = CosmosActivityPayload(
            run_id=run_id,
            project_id=project_id,
            workbook_id=workbook_id,
            agent_name=agent_name,
            activity_summary=activity_summary,
            status="success",
            timestamp=datetime.now(timezone.utc).isoformat()
        ).model_dump()

        # 3. Send to API
        headers = {"Authorization": auth_header}
        response = await resilient_async_post(ACTIVITY_API_URL, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.warning(f"Activity API rejected log: {response.text}")
            raise Exception(f"Activity API rejected log: {response.text}")

    except Exception as e:
        logger.error(f"[FALLBACK ACTIVITY LOG] {technical_message} | (API Failed: {str(e)})")
        raise e


async def send_error_to_api(
    project_id: str,
    workbook_id: str,
    run_id: str,
    agent_name: str,
    error_msg: str,
    technical_details: str,
    auth_header: str
):
    """
    Generates an error analysis using LLM and saves it to 'agent_errors'.
    """
    try:
        # 1. Generate Error Analysis (Run sync LLM call in a background thread)
        system_prompt = (
            "You are a Senior DevOps Engineer. Analyze this error. "
            "Return a JSON object with two keys: 'error_summary' "
            "and 'suggested_fix' (actionable technical advice)."
        )
        user_prompt = f"Error: {error_msg}\nTraceback/Details: {technical_details}"

        llm_response = await asyncio.to_thread(call_llm, system_prompt, user_prompt)

        # Parse LLM Response (Robustness Check)
        try:
            # Clean potential markdown code blocks
            clean_json = llm_response.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean_json)
            error_summary = parsed.get("error_summary", error_msg)
            suggested_fix = parsed.get("suggested_fix", "Check logs for details.")
            if isinstance(suggested_fix, list):
                suggested_fix = " ".join([str(x) for x in suggested_fix])
            elif not isinstance(suggested_fix, str):
                suggested_fix = str(suggested_fix)
        except (json.JSONDecodeError, ValueError):
            error_summary = f"Error: {error_msg}"
            suggested_fix = llm_response # Fallback to raw text

        # 2. Prepare Payload
        payload = CosmosErrorPayload(
            run_id=run_id,
            project_id=project_id,
            workbook_id=workbook_id,
            agent_name=agent_name,
            error_summary=error_summary,
            technical_details=technical_details,
            suggested_fix=suggested_fix,
            status="failed",
            timestamp=datetime.now(timezone.utc).isoformat()
        ).model_dump()

        # 3. Send to API
        headers = {"Authorization": auth_header}
        response = await resilient_async_post(ERROR_API_URL, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.warning(f"Error API rejected log: {response.text}")
            raise Exception(f"Error API rejected log: {response.text}")

    except Exception as e:
        logger.error(f"[FALLBACK ERROR LOG] {error_msg} | (Original Tech Details: {technical_details}) | (API/LLM Failed: {str(e)})")
        raise e
