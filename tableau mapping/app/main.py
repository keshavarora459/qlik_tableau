# Update: 2026-04-09 - Deployment trigger for Measures, Dimensions, and LODs
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.mapping import router as mapping_router
from app.core.structured_logger import CorrelationContext, get_structured_logger

# --------------------------------------------------
# APP INIT
# --------------------------------------------------
app = FastAPI(title="Mapping Engine", version="1.0.0")

logger = get_structured_logger("MAIN")

# --------------------------------------------------
# ROUTERS
# --------------------------------------------------
app.include_router(mapping_router)

# ==================================================
# GLOBAL VALIDATION ERROR HANDLER
# (Missing / invalid parameters)
# ==================================================
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError
):
    missing_params = [
        err["loc"][-1]
        for err in exc.errors()
        if err["type"] == "missing"
    ]

    if missing_params:
        message = f"Missing required parameter(s): {', '.join(missing_params)}"
    else:
        message = "Invalid request parameters"

    logger.warning(
        f"Validation error | path={request.url.path} | errors={exc.errors()}"
    )

    return JSONResponse(
        status_code=422,
        content={"detail": message, "correlation": CorrelationContext.as_dict()}
    )

# ==================================================
# GLOBAL UNHANDLED EXCEPTION HANDLER
# (Last safety net)
# ==================================================
@app.exception_handler(Exception)
async def global_exception_handler(
    request: Request,
    exc: Exception
):
    logger.critical(
        f"Unhandled error | path={request.url.path} | error={str(exc)}",
        exc_info=True
    )

    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "correlation": CorrelationContext.as_dict()}
    )
