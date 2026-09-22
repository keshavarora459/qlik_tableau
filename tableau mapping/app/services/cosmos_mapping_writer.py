from datetime import datetime, timezone

from fastapi import HTTPException

from app.core.config import settings
from app.core.dead_letter import write_dead_letter
from app.core.resilient_http import resilient_post
from app.core.structured_logger import get_structured_logger
from app.models.cosmos_contracts import CosmosMappingPayload

logger = get_structured_logger(__name__)

COSMOS_MAPPING_STORE_URL = f"{settings.cosmosdb_api_url.rstrip('/')}/mapping"

# ---------------------------------------------------------
# JSON Safety Normalizer
# Prevents crashes when payload has non-JSON objects
# ---------------------------------------------------------
def _normalize_for_json(obj):
    try:
        if obj is None:
            return None

        if isinstance(obj, (str, int, float, bool)):
            return obj

        if isinstance(obj, set):
            return list(obj)

        if isinstance(obj, list):
            return [_normalize_for_json(x) for x in obj]

        if isinstance(obj, dict):
            return {k: _normalize_for_json(v) for k, v in obj.items()}

        if hasattr(obj, "__dict__"):
            return _normalize_for_json(obj.__dict__)

        return str(obj)

    except Exception as e:
        logger.warning(
            f"Normalization fallback for {type(obj)}: {e}",
            extra={"error_type": type(e).__name__, "error_message": str(e)}
        )
        return str(obj)

# ---------------------------------------------------------
# Store Mapping Results → CosmosDB
# ---------------------------------------------------------
def store_mapping_in_cosmos(payload: dict, auth_header: str) -> dict | None:
    """
    Sends mapping results to CosmosDB API.
    Safe version — handles errors and bad data.
    """

    if not COSMOS_MAPPING_STORE_URL:
        logger.error("COSMOS_MAPPING_STORE_URL not set — skipping write")
        return None

    try:
        # Make payload JSON safe
        safe_payload = _normalize_for_json(payload)

        if not isinstance(safe_payload, dict):
            safe_payload = {"raw_content": str(safe_payload)}

        metadata = safe_payload

        cosmos_body = CosmosMappingPayload(
            run_id=metadata.get("run_id", "Unknown"),
            project_id=metadata.get("project_id", "Unknown"),
            project_name=metadata.get("project_name", "Unknown"),
            workbook_id=metadata.get("workbook_id", "Unknown"),
            status="completed",
            created_at=datetime.now(timezone.utc).isoformat(),
            payload=metadata.get("payload", {})
        ).model_dump()

        logger.info("Sending mapping results to CosmosDB")
        logger.info(f"Cosmos payload being stored: {cosmos_body}")

        headers = {"Authorization": auth_header}

        import time
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = resilient_post(
                    COSMOS_MAPPING_STORE_URL,
                    json=cosmos_body,
                    headers=headers,
                    timeout=(5.0, 45.0),
                    max_retries=3,
                    use_circuit_breaker=True
                )

                if response.status_code == 200:
                    logger.info("✅ Mapping stored in CosmosDB")
                    return response.json()

                logger.warning(f"Cosmos write failed on attempt {attempt}: {response.status_code} {response.text}")

                if attempt == max_attempts:
                    logger.error(f"Cosmos write failed after {max_attempts} attempts: {response.status_code} {response.text}")
                    write_dead_letter(
                        target_url=COSMOS_MAPPING_STORE_URL,
                        payload=cosmos_body,
                        error=f"{response.status_code} {response.text}",
                        source_function="store_mapping_in_cosmos"
                    )
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=response.text
                    )

            except Exception as e:
                if isinstance(e, HTTPException):
                    raise

                logger.warning(f"Exception during Cosmos write on attempt {attempt}: {str(e)}")
                if attempt == max_attempts:
                    raise

            # Wait before retrying (exponential backoff)
            time.sleep(2 ** attempt)

    except Exception as e:
        if not isinstance(e, HTTPException):
            logger.error(
                f"❌ Unexpected error writing to CosmosDB: {str(e)}",
                extra={"error_type": type(e).__name__, "error_message": str(e)}
            )
            try:
                write_dead_letter(
                    target_url=COSMOS_MAPPING_STORE_URL,
                    payload={"payload": payload},
                    error=str(e),
                    source_function="store_mapping_in_cosmos"
                )
            except Exception:
                pass

            raise

        raise
