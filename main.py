import os
import sys
import json
import asyncio
import datetime
import traceback
import importlib.util
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response

app = FastAPI(title="Unified Mapping Router", version="2.0.0")
base_dir = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------
# Load Environment Variables
# ---------------------------------------------------------
from dotenv import load_dotenv
load_dotenv(os.path.join(base_dir, ".env"))

# ---------------------------------------------------------
# Setup Module Paths Safely
# ---------------------------------------------------------
qlik_dir = os.path.join(base_dir, "qlik mapping")

sys.path.insert(0, qlik_dir)

# ---------------------------------------------------------
# Import Qlik natively
# ---------------------------------------------------------
try:
    # We must use importlib for Qlik's app.py to avoid `import app` colliding with Tableau's folder
    spec_app = importlib.util.spec_from_file_location("qlik_app_module", os.path.join(qlik_dir, "app.py"))
    qlik_app_module = importlib.util.module_from_spec(spec_app)
    sys.modules["qlik_app_module"] = qlik_app_module
    spec_app.loader.exec_module(qlik_app_module)

    qlik_post_mapping_data = qlik_app_module.post_mapping_data
    QlikMappingRequest = qlik_app_module.MappingRequest
    print("Successfully loaded Qlik agent natively.")
except Exception as e:
    print(f"Error loading Qlik: {e}")
    traceback.print_exc()

# ---------------------------------------------------------
# Unified Native Router
# ---------------------------------------------------------
@app.post("/api/unified-mapping")
async def unified_mapping(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    source_type = body.get("source_type")
    if not source_type:
        raise HTTPException(status_code=400, detail="Missing 'source_type' in payload. Must be 'qlik'.")

    # Route natively
    try:
        if source_type == "qlik":
            req_obj = QlikMappingRequest(**body)
            # Call Qlik's native function
            resp_data = await qlik_post_mapping_data(payload=req_obj, request=request)
            status_code = 200
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported source_type: {source_type}")
            
    except HTTPException as he:
        # Agent raised a native HTTP error (like 400 Bad Request for missing run_id)
        resp_data = he.detail
        status_code = he.status_code
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal agent error: {str(e)}")

    # Add timestamp
    timestamp = datetime.datetime.utcnow().isoformat()
    if isinstance(resp_data, dict):
        resp_data["saved_at"] = timestamp
        
    run_id = body.get("run_id", "unknown_run_id")
    
    # Wrap the response in an envelope (the "res things" requested by the user)
    final_response = {
        "status": "success" if status_code == 200 else "error",
        "message": "Mapping processed successfully" if status_code == 200 else "An error occurred during mapping",
        "run_id": run_id,
        "source_type": source_type,
        "mapping_result": resp_data
    }
    
    safe_timestamp = timestamp.replace(":", "-")
    
    # Save all responses in a subfolder named 'mapping result'
    save_dir = os.path.join(base_dir, "mapping result")
    os.makedirs(save_dir, exist_ok=True)
    
    with open(os.path.join(save_dir, f"{run_id}_{safe_timestamp}.json"), "w", encoding="utf-8") as f:
        json.dump(final_response, f, indent=4)
    
    return Response(
        content=json.dumps(final_response),
        status_code=status_code,
        media_type="application/json"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
