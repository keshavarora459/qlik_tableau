# Update: 2026-04-09 - Deployment trigger for Measures, Dimensions, and LODs
import requests

from app.core.config import settings
from app.core.resilient_http import resilient_get
from app.core.structured_logger import get_structured_logger

# 1️⃣ IMPORT THE LOGGERS
from app.services.cosmos_log_service import store_activity, store_error, store_log

logger = get_structured_logger(__name__)

def fetch_data_layer_result(
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str
) -> dict:
    """
    Fetch data layer results from Data Layer Agent using
    project_id, workbook_id, and run_id.

    Guarantees:
    - Correct project/workbook/run is fetched
    - Returns a SINGLE data layer document (dict)
    """

    # 2️⃣ LOG ACTIVITY: Agent starts fetching data
    store_activity(
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        technical_message="Fetching raw JSON data layer results from upstream Data Layer Agent API.",
        auth_header=auth_header
    )

    url = settings.data_layer_results_base_url

    if not url:
        logger.error("data_layer_results_base_url is not set")
        return {}

    params = {
        "project_id": project_id,
        "workbook_id": workbook_id,
        "run_id": run_id
    }
    headers = {
        "Authorization": auth_header
    }

    try:
        response = resilient_get(
            url,
            params=params,
            headers=headers,
            timeout=(5.0, 30.0),
            max_retries=3
        )
        response.raise_for_status()

    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 500
        message = "Unknown upstream error"

        if exc.response is not None:
            try:
                resp_json = exc.response.json()
                if isinstance(resp_json, dict):
                    message = resp_json.get("detail", resp_json.get("message", resp_json.get("error", "Unknown upstream error")))
                else:
                    message = str(resp_json)
            except ValueError:
                message = exc.response.text.strip() or "Unknown upstream error"

        # LOG ERROR: API failure
        try:
            store_error(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                error_msg=f"HTTPError {status_code} fetching data layer results from Data Layer API.",
                technical_details=f"URL: {exc.request.url if hasattr(exc, 'request') and exc.request else url} | Body: {message}",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log data layer error to Cosmos: {message}", extra={"error_type": type(e).__name__, "error_message": str(e)})

        # Return empty dict instead of raising error as requested by user
        return {}

    except requests.RequestException as exc:
        # LOG ERROR: Network failure
        try:
            store_error(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                error_msg="Connection error while fetching from Data Layer API.",
                technical_details=str(exc),
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log connection error: {exc}", extra={"error_type": type(e).__name__, "error_message": str(e)})

        # Return empty dict instead of raising error
        return {}

    try:
        data_layer_response = response.json()
    except ValueError as exc:
        # LOG ERROR: Bad JSON
        try:
            store_error(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                error_msg="Invalid JSON received from Data Layer Agent.",
                technical_details=str(exc),
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log JSON parse error: {exc}", extra={"error_type": type(e).__name__, "error_message": str(e)})

        # Return empty dict instead of raising error
        return {}

    # --------------------------------------------------
    # Normalize list response (if applicable)
    # --------------------------------------------------
    if isinstance(data_layer_response, list):
        if not data_layer_response:
            # Maybe it's NOT an error for data layer, but let's follow the parsing pattern
            store_log(
                project_id=project_id,
                project_name="Unknown",
                workbook_id=workbook_id,
                run_id=run_id,
                function_name="fetch_data_layer_result",
                log_level="INFO",
                message="No data layer results found for given project/workbook/run. Returning empty dict.",
                auth_header=auth_header
            )
            return {}
        data_layer_response = data_layer_response[0]

    if not isinstance(data_layer_response, dict):
        try:
            store_error(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                error_msg="Unexpected data layer response format.",
                technical_details=f"Expected a dict, but got {type(data_layer_response).__name__}",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error("Failed to log data layer format error", extra={"error_type": type(e).__name__, "error_message": str(e)})
        return {}

    # STANDARD LOG: Success
    store_log(
        project_id=project_id,
        project_name="Unknown",  # Or parse project_name out if it exists
        workbook_id=workbook_id,
        run_id=run_id,
        function_name="fetch_data_layer_result",
        log_level="INFO",
        message="Successfully fetched and validated data layer payload.",
        auth_header=auth_header
    )

    return data_layer_response
