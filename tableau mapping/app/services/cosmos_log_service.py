import asyncio
import threading

from app.core.dead_letter import write_dead_letter
from app.core.structured_logger import CorrelationContext, get_structured_logger
from app.services.agent_logger import send_activity_to_api, send_error_to_api, send_log_to_api

logger = get_structured_logger(__name__)

AGENT_NAME = "MappingAgent"

background_tasks = set()

def _handle_task_result(task: asyncio.Task, target_url: str, payload: dict, source_function: str) -> None:
    """Callback to check if background task failed, and write to dead letter if so."""
    background_tasks.discard(task)
    try:
        exc = task.exception()
        if exc:
            logger.error(
                f"Background task failed for {source_function}",
                extra={"error_type": type(exc).__name__, "error_message": str(exc)}
            )
            write_dead_letter(target_url, payload, str(exc), source_function)
    except asyncio.CancelledError:
        logger.warning(f"Background task cancelled for {source_function}")

def _run_coro_in_thread(coro, target_url: str, payload: dict, source_function: str) -> None:
    def _bg_task():
        try:
            asyncio.run(coro)
        except Exception as e:
            logger.error(
                f"Background task failed for {source_function}",
                extra={"error_type": type(e).__name__, "error_message": str(e)}
            )
            write_dead_letter(target_url, payload, str(e), source_function)
    threading.Thread(target=_bg_task, daemon=True).start()


def store_log(
    project_id: str,
    project_name: str,
    workbook_id: str,
    run_id: str,
    function_name: str,
    log_level: str,
    message: str,
    auth_header: str,
    details: dict | None = None
):
    if auth_header == "Bearer SYSTEM_INIT":
        return

    # Update CorrelationContext
    CorrelationContext.set(run_id=run_id, workbook_id=workbook_id, project_id=project_id)

    payload_details = details or {}
    logger_msg = f"{function_name} | Project: {project_name or 'None'} | {message}"

    # Write to local structured log
    if log_level.upper() in ("ERROR", "CRITICAL"):
        logger.error(logger_msg, extra={"extra_data": payload_details})
    elif log_level.upper() == "WARNING":
        logger.warning(logger_msg, extra={"extra_data": payload_details})
    else:
        logger.info(logger_msg, extra={"extra_data": payload_details})

    coro = send_log_to_api(
        agent_name=AGENT_NAME,
        project_name=project_name,
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        log_level=log_level,
        message=message,
        auth_header=auth_header,
        details={
            "function_name": function_name,
            **payload_details
        }
    )

    fallback_payload = {
        "agent_name": AGENT_NAME,
        "project_id": project_id,
        "workbook_id": workbook_id,
        "run_id": run_id,
        "log_level": log_level,
        "message": message,
    }

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(lambda t: _handle_task_result(t, "api/records/logs", fallback_payload, "store_log"))
    except RuntimeError:
        _run_coro_in_thread(coro, "api/records/logs", fallback_payload, "store_log")

    except Exception as e:
        logger.error(f"Cosmos logging failed to enqueue: {e}")


def store_activity(
    project_id: str,
    workbook_id: str,
    run_id: str,
    technical_message: str,
    auth_header: str
):
    if auth_header == "Bearer SYSTEM_INIT":
        return

    CorrelationContext.set(run_id=run_id, workbook_id=workbook_id, project_id=project_id)
    logger.info(f"[ACTIVITY] {technical_message}")

    coro = send_activity_to_api(
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        agent_name=AGENT_NAME,
        technical_message=technical_message,
        auth_header=auth_header
    )

    fallback_payload = {
        "agent_name": AGENT_NAME,
        "project_id": project_id,
        "workbook_id": workbook_id,
        "run_id": run_id,
        "technical_message": technical_message,
    }

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(lambda t: _handle_task_result(t, "api/records/activities", fallback_payload, "store_activity"))
    except RuntimeError:
        _run_coro_in_thread(coro, "api/records/activities", fallback_payload, "store_activity")

    except Exception as e:
        logger.error(f"Activity logging failed to enqueue: {e}")


def store_error(
    project_id: str,
    workbook_id: str,
    run_id: str,
    error_msg: str,
    technical_details: str,
    auth_header: str
):
    if auth_header == "Bearer SYSTEM_INIT":
        return

    CorrelationContext.set(run_id=run_id, workbook_id=workbook_id, project_id=project_id)
    logger.error(f"[ERROR] {error_msg}", extra={"extra_data": {"technical_details": technical_details}})

    coro = send_error_to_api(
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        agent_name=AGENT_NAME,
        error_msg=error_msg,
        technical_details=technical_details,
        auth_header=auth_header
    )

    fallback_payload = {
        "agent_name": AGENT_NAME,
        "project_id": project_id,
        "workbook_id": workbook_id,
        "run_id": run_id,
        "error_msg": error_msg,
        "technical_details": technical_details
    }

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(lambda t: _handle_task_result(t, "api/records/errors", fallback_payload, "store_error"))
    except RuntimeError:
        _run_coro_in_thread(coro, "api/records/errors", fallback_payload, "store_error")

    except Exception as e:
        logger.error(f"Error logging failed to enqueue: {e}")

