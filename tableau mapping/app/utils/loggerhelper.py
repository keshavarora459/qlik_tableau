from app.core.logger import get_logger

# 1️⃣ UPDATE IMPORT: Point to your new Cosmos log service instead of the old one
from app.services.cosmos_log_service import store_log


# 2️⃣ UPDATE SIGNATURE: Add workbook_id, run_id, and auth_header
def log_event(
    logger_name: str,
    project_id: str,
    project_name: str,
    workbook_id: str,  # 👈 NEW
    run_id: str,       # 👈 NEW
    function_name: str,
    status: str,
    message: str,
    auth_header: str   # 👈 NEW
):
    try:
        logger = get_logger(logger_name)

        log_msg = f"[{function_name}] {message}"

        # Convert your old 'status' (like "SUCCESS" or "ERROR") to standard log levels
        log_level = "ERROR" if status.upper() == "ERROR" else "INFO"

        if log_level == "ERROR":
            logger.error(log_msg)
        else:
            logger.info(log_msg)

        # 3️⃣ CALL NEW STORE_LOG: Pass all the required tracking variables to Cosmos DB
        store_log(
            project_id=project_id,
            project_name=project_name,
            workbook_id=workbook_id,
            run_id=run_id,
            function_name=function_name,
            log_level=log_level,
            message=message,
            auth_header=auth_header
        )
    except Exception as e:
        # Fallback: never let a logging failure crash the caller
        from app.core.structured_logger import get_structured_logger
        fallback_logger = get_structured_logger(__name__)
        fallback_logger.error(f"log_event failed for [{function_name}] {message}: {e}", extra={"error_type": type(e).__name__})
