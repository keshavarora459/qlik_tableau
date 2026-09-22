import logging

from app.services.cosmos_log_service import store_activity, store_error, store_log

# Since logger initialization happens at the system level,
# we use "SYSTEM" placeholders for the required tracking IDs.
SYSTEM_ID = "SYSTEM_STARTUP"
DUMMY_AUTH = "Bearer SYSTEM_INIT"

def get_logger(name: str):
    try:
        # 📝 1. LOG ACTIVITY: Starting logger initialization
        store_activity(
            project_id=SYSTEM_ID,
            workbook_id=SYSTEM_ID,
            run_id=SYSTEM_ID,
            technical_message=f"Initializing standard system logger for module: {name}",
            auth_header=DUMMY_AUTH
        )

        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        # 📝 2. STANDARD LOG: Success
        store_log(
            project_id=SYSTEM_ID,
            project_name="System",
            workbook_id=SYSTEM_ID,
            run_id=SYSTEM_ID,
            function_name="get_logger",
            log_level="INFO",
            message=f"Logger '{name}' configured successfully.",
            auth_header=DUMMY_AUTH
        )

        return logger

    except Exception as e:
        # 🚨 3. LOG ERROR: Catch initialization failures
        store_error(
            project_id=SYSTEM_ID,
            workbook_id=SYSTEM_ID,
            run_id=SYSTEM_ID,
            error_msg=f"Critical failure while configuring logger for '{name}'.",
            technical_details=str(e),
            auth_header=DUMMY_AUTH
        )

        # Re-raise the exception because if logging fails, we need to know immediately
        raise e
