"""
CovertOps - Asynchronous Background Consumer Daemon

This module runs a background worker thread that decouples disk I/O and database
writes from the real-time vision detection loop. It consumes incident payloads
from the in-memory event queue, compresses and persists facial snapshots to disk,
and records telemetry and audit logs into the SQLite database.
"""

import threading
import logging
import time
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import cv2
import numpy as np

from config import SNAPSHOTS_DIR
from core.event_queue import event_bus
from background.database import telemetry_db

logger = logging.getLogger("covertops.worker")


class BackgroundWorker:
    """Background consumer daemon processing incident events from the event bus.

    Attributes:
        _running (bool): State flag controlling the daemon execution loop.
        _thread (Optional[threading.Thread]): The background consumer thread instance.
    """

    def __init__(self) -> None:
        """Initialize the background worker and ensure snapshot storage directory exists."""
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None

        # Ensure the destination directory for captured snapshots exists on disk
        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        logger.debug("BackgroundWorker initialized with SNAPSHOTS_DIR: %s", SNAPSHOTS_DIR)

    @property
    def is_running(self) -> bool:
        """Check whether the worker loop is actively running.

        Returns:
            bool: True if the worker is running and the thread is alive, False otherwise.
        """
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Create and start the background consumer daemon thread."""
        if self._running:
            logger.warning("BackgroundWorker is already running.")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="CovertOps-BackgroundWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("BackgroundWorker daemon thread started successfully.")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker loop, join the thread, and drain any remaining items.

        Args:
            timeout (float): Max time in seconds to wait for the thread to join.
                             Defaults to 5.0 seconds.
        """
        if not self._running and self._thread is None:
            return

        logger.info("Stopping BackgroundWorker daemon...")
        self._running = False

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "BackgroundWorker thread did not terminate within %s seconds.", timeout
                )
        self._thread = None

        # Drain any remaining incident items from the event bus
        remaining_items: List[Dict[str, Any]] = event_bus.drain()
        if remaining_items:
            logger.info(
                "Draining %d remaining incident payload(s) from event bus.", len(remaining_items)
            )
            self._process_drain(remaining_items)

        logger.info("BackgroundWorker daemon stopped.")

    def _run_loop(self) -> None:
        """Continuously consume incident payloads from the event bus until stopped."""
        logger.info("BackgroundWorker consumer loop entered.")
        while self._running:
            try:
                payload = event_bus.consume(timeout=1.0)
                if payload is not None:
                    self._process_payload(payload)
            except Exception as exc:
                logger.error(
                    "Unexpected error in BackgroundWorker consumption loop: %s",
                    exc,
                    exc_info=True,
                )
        logger.info("BackgroundWorker consumer loop exited.")

    def _process_drain(self, items: List[Dict[str, Any]]) -> None:
        """Process remaining items drained from the event queue upon shutdown.

        Args:
            items (List[Dict[str, Any]]): List of unprocessed incident payloads.
        """
        for item in items:
            if isinstance(item, dict):
                self._process_payload(item)
            else:
                logger.warning("Skipping invalid item in drained event list: %r", item)

    def _process_payload(self, payload: Dict[str, Any]) -> None:
        """Process a single incident payload: persist snapshot and log telemetry.

        Args:
            payload (Dict[str, Any]): Dictionary containing incident data:
                - timestamp (float | datetime | None): Time of event occurrence.
                - threat_type (str): Classification label ('LEAD_SUPERVISOR', etc.).
                - reaction_latency_ms (float | None): Pipeline latency in milliseconds.
                - frame_crop (np.ndarray | None): Cropped visitor face image.
                - subject_name (str | None): Identified person name or None.
                - similarity_score (float | None): Embedding cosine similarity.
                - proximity_ratio (float | None): Bounding box / frame area ratio.
        """
        try:
            # 1. Extract visitor crop and timestamp
            frame_crop = payload.get("frame_crop")
            raw_ts = payload.get("timestamp")

            if isinstance(raw_ts, (int, float)) and raw_ts > 0:
                dt = datetime.fromtimestamp(raw_ts)
            elif isinstance(raw_ts, datetime):
                dt = raw_ts
            else:
                dt = datetime.now()

            timestamp_str = dt.strftime("%Y%m%d_%H%M%S_%f")
            filename = f"incident_{timestamp_str}.jpg"

            # 2. Save snapshot to SNAPSHOTS_DIR with JPEG quality 85 if valid
            snapshot_path: Optional[str] = None
            if frame_crop is not None and isinstance(frame_crop, np.ndarray) and frame_crop.size > 0:
                full_path = Path(SNAPSHOTS_DIR) / filename
                success = cv2.imwrite(
                    str(full_path),
                    frame_crop,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 85],
                )
                if success:
                    # Relative path for portable storage in database and dashboard URLs
                    snapshot_path = f"snapshots/{filename}"
                else:
                    logger.warning("cv2.imwrite failed to write snapshot to %s", full_path)

            # 3. Extract and map telemetry fields
            classification = payload.get("threat_type", "UNKNOWN")
            subject_name = payload.get("subject_name")
            similarity_score = payload.get("similarity_score")
            proximity_ratio = payload.get("proximity_ratio")
            detection_latency_ms = payload.get("reaction_latency_ms")
            dwell_duration_seconds: Optional[float] = None

            # 4. Persist incident record into SQLite database
            incident_id = telemetry_db.log_incident(
                classification=classification,
                subject_name=subject_name,
                similarity_score=similarity_score,
                proximity_ratio=proximity_ratio,
                detection_latency_ms=detection_latency_ms,
                dwell_duration_seconds=dwell_duration_seconds,
                snapshot_path=snapshot_path,
            )

            # 5. Log summary at INFO level
            logger.info(
                "Logged incident (id=%s): classification=%s, subject=%s, similarity=%s, "
                "proximity=%s, latency_ms=%s, snapshot=%s",
                incident_id,
                classification,
                subject_name,
                f"{similarity_score:.3f}" if isinstance(similarity_score, (int, float)) else "N/A",
                f"{proximity_ratio:.3f}" if isinstance(proximity_ratio, (int, float)) else "N/A",
                f"{detection_latency_ms:.1f}" if isinstance(detection_latency_ms, (int, float)) else "N/A",
                snapshot_path,
            )

        except Exception as exc:
            logger.error("Failed to process incident payload: %s", exc, exc_info=True)


# Module-level singleton instance
background_worker = BackgroundWorker()

__all__ = ["BackgroundWorker", "background_worker"]
