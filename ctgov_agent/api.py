"""HTTP surface.

POST /v1/visualize   VizRequest -> VizResponse
GET  /v1/schema      JSON Schemas for request and response
GET  /health
GET  /               demo UI (web/index.html)
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .ctgov.client import CTGovError
from .planner.plan import PlanError
from .schemas import VizRequest, VizResponse
from .service import VizService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="ClinicalTrials.gov Query-to-Visualization Agent", version="0.1.0")
WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


@lru_cache(maxsize=1)
def get_service() -> VizService:
    return VizService()


@app.post("/v1/visualize", response_model=VizResponse, response_model_exclude_none=True)
def visualize(req: VizRequest) -> VizResponse:
    # sync def: FastAPI runs it in a threadpool, so blocking httpx calls don't stall the loop
    try:
        return get_service().run(req)
    except PlanError as e:
        raise HTTPException(status_code=422, detail={"error": "unplannable_query", "message": str(e)})
    except CTGovError as e:
        raise HTTPException(status_code=502, detail={"error": "upstream_error", "message": str(e)})


@app.get("/v1/schema")
def schema() -> dict:
    return {"request": VizRequest.model_json_schema(), "response": VizResponse.model_json_schema()}


@app.get("/health")
def health() -> dict:
    svc = get_service()
    return {"status": "ok", "planner": getattr(svc.planner, "name", "unknown")}


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(WEB)
