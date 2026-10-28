"""
CovertOps - FastAPI REST API Backend

This module serves telemetry data, security incidents, aggregate latency metrics,
and cropped visitor face snapshots to the React dashboard interface. It also
provides endpoints for human-in-the-loop reclassification (tagging) of detected subjects.
"""

import logging
import os
from pathlib import Path
import sys
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Ensure root directory is on sys.path for direct script execution
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import SNAPSHOTS_DIR, API_HOST, API_PORT
from background.database import TelemetryDB

logger = logging.getLogger("covertops.api")

# Allowed classification categories for subject tagging
ALLOWED_CLASSIFICATIONS = {"LEAD_SUPERVISOR", "COLLEAGUE", "UNKNOWN"}

# Initialize FastAPI application
app = FastAPI(
    title="CovertOps Telemetry API",
    version="1.0.0",
    description="Telemetry, incident logging, and snapshot server for CovertOps workstation camouflage.",
)

# Enable CORS for local dashboard development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Module-level database instance for API queries
db = TelemetryDB()


# --- Pydantic Data Models ---

class IncidentResponse(BaseModel):
    """Schema for a single security incident record with derived snapshot URL."""
    incident_id: int
    timestamp: str
    classification: str
    subject_name: Optional[str] = None
    similarity_score: Optional[float] = None
    proximity_ratio: Optional[float] = None
    detection_latency_ms: Optional[float] = None
    dwell_duration_seconds: Optional[float] = None
    snapshot_path: Optional[str] = None
    snapshot_url: Optional[str] = None  # Computed endpoint URL


class MetricsResponse(BaseModel):
    """Schema for aggregate telemetry metrics and latency percentiles."""
    total_incidents: int
    total_today: int
    p50_latency_ms: Optional[float] = None
    p95_latency_ms: Optional[float] = None
    p99_latency_ms: Optional[float] = None
    avg_dwell_seconds: Optional[float] = None


class TagFaceRequest(BaseModel):
    """Payload schema for reclassifying an incident subject."""
    incident_id: int
    new_classification: str  # 'LEAD_SUPERVISOR', 'COLLEAGUE', 'UNKNOWN'
    subject_name: Optional[str] = None


class TagFaceResponse(BaseModel):
    """Response schema for tag-face mutation operations."""
    success: bool
    message: str


# --- Helper Functions ---

def _compute_snapshot_url(snapshot_path: Optional[str]) -> Optional[str]:
    """Derive the API snapshot endpoint URL from a local file path.

    Args:
        snapshot_path: Local filesystem path string, or None.

    Returns:
        The URL path string (e.g. '/api/snapshots/visitor.jpg') or None.
    """
    if not snapshot_path:
        return None
    cleaned = str(snapshot_path).strip()
    if not cleaned:
        return None
    filename = Path(cleaned).name
    return f"/api/snapshots/{filename}"


def _format_incident(record: dict) -> IncidentResponse:
    """Format raw database incident record dictionary into IncidentResponse model.

    Args:
        record: Dictionary representation of a security_incidents database row.

    Returns:
        An instantiated IncidentResponse Pydantic model.
    """
    snapshot_path = record.get("snapshot_path")
    snapshot_url = record.get("snapshot_url") or _compute_snapshot_url(snapshot_path)

    incident_id = record.get("incident_id")
    if incident_id is None:
        incident_id = record.get("id", 0)

    classification = record.get("classification")
    if classification is None:
        classification = record.get("threat_type", "UNKNOWN")

    detection_latency = record.get("detection_latency_ms")
    if detection_latency is None:
        detection_latency = record.get("reaction_latency_ms", record.get("latency_ms"))

    dwell_duration = record.get("dwell_duration_seconds")
    if dwell_duration is None:
        dwell_duration = record.get("dwell_time", record.get("dwell_duration"))

    return IncidentResponse(
        incident_id=int(incident_id),
        timestamp=str(record.get("timestamp", "")),
        classification=str(classification),
        subject_name=record.get("subject_name"),
        similarity_score=record.get("similarity_score"),
        proximity_ratio=record.get("proximity_ratio"),
        detection_latency_ms=detection_latency,
        dwell_duration_seconds=dwell_duration,
        snapshot_path=snapshot_path,
        snapshot_url=snapshot_url,
    )


# --- Lifecycle Events ---

@app.on_event("startup")
async def startup_event() -> None:
    """Log server startup initialization."""
    logger.info("CovertOps API server started")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Safely close database connections upon API server shutdown."""
    logger.info("CovertOps API server shutting down...")
    try:
        db.close()
    except Exception as exc:
        logger.error("Error closing database during shutdown: %s", exc, exc_info=True)


# --- API Endpoints ---

@app.get("/api/incidents", response_model=list[IncidentResponse])
async def list_incidents(
    limit: int = Query(50, ge=1, le=200, description="Maximum number of incidents to return"),
    offset: int = Query(0, ge=0, description="Offset index for pagination"),
    classification: Optional[str] = Query(None, description="Optional classification filter"),
):
    """Return a paginated list of security incidents ordered by timestamp descending.

    Args:
        limit: Max incident records to fetch (1 to 200).
        offset: Record offset for pagination.
        classification: Optional category filter ('LEAD_SUPERVISOR', 'COLLEAGUE', 'UNKNOWN').

    Returns:
        List of IncidentResponse objects with computed snapshot URLs.
    """
    try:
        rows = db.get_incidents(limit=limit, offset=offset, classification=classification)
        return [_format_incident(row) for row in rows]
    except Exception as exc:
        logger.error("Failed to query incidents from database: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve incidents")


@app.get("/api/metrics", response_model=MetricsResponse)
async def get_metrics():
    """Return aggregate telemetry metrics and detection latency percentiles.

    Returns:
        MetricsResponse containing total incident counts and p50/p95/p99 latencies.
    """
    try:
        metrics = db.get_metrics()
        return MetricsResponse(
            total_incidents=metrics.get("total_incidents", 0),
            total_today=metrics.get("total_today", 0),
            p50_latency_ms=metrics.get("p50_latency_ms"),
            p95_latency_ms=metrics.get("p95_latency_ms"),
            p99_latency_ms=metrics.get("p99_latency_ms"),
            avg_dwell_seconds=metrics.get("avg_dwell_seconds"),
        )
    except Exception as exc:
        logger.error("Failed to compute telemetry metrics: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve metrics")


@app.get("/api/snapshots/{filename}", response_class=FileResponse)
async def get_snapshot(filename: str):
    """Serve a visitor snapshot image with path traversal protection.

    Args:
        filename: Snapshot image filename (e.g. 'visitor_123.jpg').

    Returns:
        FileResponse streaming the JPEG image data.
    """
    try:
        snapshots_dir_resolved = SNAPSHOTS_DIR.resolve()
        file_path = (snapshots_dir_resolved / filename).resolve()

        # Prevent directory traversal attacks
        if not file_path.is_relative_to(snapshots_dir_resolved) or file_path == snapshots_dir_resolved:
            logger.warning("Path traversal attempt detected: filename=%s", filename)
            raise HTTPException(status_code=400, detail="Invalid snapshot path")
    except (ValueError, TypeError):
        logger.warning("Invalid snapshot path resolution: filename=%s", filename)
        raise HTTPException(status_code=400, detail="Invalid snapshot path")

    if not file_path.is_file():
        logger.info("Snapshot file not found: %s", filename)
        raise HTTPException(status_code=404, detail="Snapshot not found")

    return FileResponse(path=str(file_path), media_type="image/jpeg")


@app.post("/api/tag-face", response_model=TagFaceResponse)
async def tag_face(request: TagFaceRequest):
    """Reclassify a recorded incident subject.

    Args:
        request: TagFaceRequest containing incident_id, new_classification, and optional subject_name.

    Returns:
        TagFaceResponse indicating mutation success.
    """
    if request.new_classification not in ALLOWED_CLASSIFICATIONS:
        logger.warning(
            "Invalid classification '%s' requested for incident %d",
            request.new_classification,
            request.incident_id,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid classification '{request.new_classification}'. "
                f"Allowed values are: {sorted(ALLOWED_CLASSIFICATIONS)}"
            ),
        )

    try:
        updated = db.update_classification(
            incident_id=request.incident_id,
            new_classification=request.new_classification,
            new_subject_name=request.subject_name,
        )
    except Exception as exc:
        logger.error(
            "Error updating classification for incident %d: %s",
            request.incident_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to update classification")

    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"Incident with ID {request.incident_id} not found",
        )

    return TagFaceResponse(
        success=True,
        message=f"Incident {request.incident_id} successfully reclassified as {request.new_classification}",
    )


@app.get("/api/incidents/{incident_id}", response_model=IncidentResponse)
async def get_incident(incident_id: int):
    """Retrieve detailed metadata for a single security incident by its ID.

    Args:
        incident_id: Primary key ID of the incident.

    Returns:
        IncidentResponse object with computed snapshot URL.
    """
    try:
        record = db.get_incident_by_id(incident_id)
    except Exception as exc:
        logger.error("Error querying incident %d: %s", incident_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve incident")

    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Incident with ID {incident_id} not found",
        )

    return _format_incident(record)


@app.get("/api/health")
async def health_check():
    """Health check endpoint for dashboard connection monitoring."""
    return {"status": "ok", "service": "CovertOps Telemetry API"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.api:app", host=API_HOST, port=API_PORT, reload=False)
