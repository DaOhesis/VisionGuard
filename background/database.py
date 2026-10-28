"""
CovertOps - SQLite Telemetry and Incident Persistence Module

This module manages the persistent SQLite database for the CovertOps system,
recording security incident triggers, telemetry metrics, and diagnostic logs.
It provides thread-safe access allowing real-time background worker writes
and concurrent FastAPI reader queries.
"""

import logging
import os
from pathlib import Path
import sqlite3
import sys
import threading

# Ensure root project directory is accessible for imports
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import DB_PATH

logger = logging.getLogger("covertops.database")


def _calculate_percentile(sorted_data: list[float], percentile: float) -> float:
    """Calculate the p-th percentile of a sorted list of numeric values.

    Uses linear interpolation between adjacent ranks.

    Args:
        sorted_data: List of numbers sorted in ascending order.
        percentile: Float between 0.0 and 100.0.

    Returns:
        Interpolated percentile value rounded to 2 decimal places, or 0.0 if empty.
    """
    if not sorted_data:
        return 0.0
    if len(sorted_data) == 1:
        return float(sorted_data[0])

    k = (len(sorted_data) - 1) * (percentile / 100.0)
    floor_k = int(k)
    ceil_k = min(floor_k + 1, len(sorted_data) - 1)

    if floor_k == ceil_k:
        return float(sorted_data[floor_k])

    d0 = sorted_data[floor_k] * (ceil_k - k)
    d1 = sorted_data[ceil_k] * (k - floor_k)
    return round(float(d0 + d1), 2)


class TelemetryDB:
    """Thread-safe SQLite database manager for security incidents and telemetry."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        """Initialize the database connection and schema.

        Args:
            db_path: Optional custom path to the SQLite database file.
                     Defaults to DB_PATH configured in config.py.
        """
        self.db_path = Path(db_path) if db_path is not None else Path(DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # check_same_thread=False allows writer threads and reader threads to share connection
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        # Enable Write-Ahead Logging (WAL) for better concurrent read/write throughput
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.Error as exc:
            logger.warning("Failed to enable WAL mode: %s", exc)

        self._init_schema()
        logger.info("TelemetryDB initialized at %s", self.db_path)

    def _init_schema(self) -> None:
        """Create necessary SQLite tables if they do not exist."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS security_incidents (
                    incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    classification TEXT NOT NULL,
                    subject_name TEXT,
                    similarity_score REAL,
                    proximity_ratio REAL,
                    detection_latency_ms REAL,
                    dwell_duration_seconds REAL,
                    snapshot_path TEXT
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry_diagnostics (
                    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    loop_fps REAL,
                    cpu_utilization_pct REAL,
                    ram_usage_mb REAL
                );
                """
            )
            self._conn.commit()

    def log_incident(
        self,
        classification: str,
        subject_name: str | None,
        similarity_score: float | None,
        proximity_ratio: float | None,
        detection_latency_ms: float | None,
        dwell_duration_seconds: float | None,
        snapshot_path: str | None,
    ) -> int:
        """Log a new security incident record.

        Args:
            classification: Classification type (e.g. 'LEAD_SUPERVISOR', 'COLLEAGUE', 'UNKNOWN').
            subject_name: Identified subject name if matched, else None.
            similarity_score: Face recognition cosine similarity score or None.
            proximity_ratio: Bounding box area over frame area ratio or None.
            detection_latency_ms: Detection & classification elapsed time in milliseconds.
            dwell_duration_seconds: Time subject remained in proximity frame.
            snapshot_path: Local filesystem path to the saved cropped snapshot image.

        Returns:
            The auto-generated incident_id integer.
        """
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO security_incidents (
                    classification,
                    subject_name,
                    similarity_score,
                    proximity_ratio,
                    detection_latency_ms,
                    dwell_duration_seconds,
                    snapshot_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    classification,
                    subject_name,
                    similarity_score,
                    proximity_ratio,
                    detection_latency_ms,
                    dwell_duration_seconds,
                    snapshot_path,
                ),
            )
            self._conn.commit()
            incident_id = cursor.lastrowid
            logger.debug("Logged security incident #%d [%s]", incident_id, classification)
            return int(incident_id)

    def log_diagnostics(
        self,
        loop_fps: float,
        cpu_utilization_pct: float,
        ram_usage_mb: float,
    ) -> None:
        """Insert a system diagnostic snapshot.

        Args:
            loop_fps: Current vision processing loop frames per second.
            cpu_utilization_pct: Total CPU utilization percentage.
            ram_usage_mb: Resident process RAM usage in megabytes.
        """
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO telemetry_diagnostics (
                    loop_fps,
                    cpu_utilization_pct,
                    ram_usage_mb
                ) VALUES (?, ?, ?)
                """,
                (loop_fps, cpu_utilization_pct, ram_usage_mb),
            )
            self._conn.commit()
            logger.debug(
                "Logged telemetry diagnostics: fps=%.2f cpu=%.1f%% ram=%.1fMB",
                loop_fps,
                cpu_utilization_pct,
                ram_usage_mb,
            )

    def get_incidents(
        self,
        limit: int = 50,
        offset: int = 0,
        classification: str | None = None,
    ) -> list[dict]:
        """Fetch recorded security incidents ordered by timestamp descending.

        Args:
            limit: Maximum number of records to return.
            offset: Number of records to skip for pagination.
            classification: Optional classification filter string.

        Returns:
            List of incident dictionaries.
        """
        with self._lock:
            cursor = self._conn.cursor()
            if classification is not None:
                cursor.execute(
                    """
                    SELECT * FROM security_incidents
                    WHERE classification = ?
                    ORDER BY timestamp DESC, incident_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (classification, limit, offset),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM security_incidents
                    ORDER BY timestamp DESC, incident_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_metrics(self) -> dict:
        """Compute aggregate telemetry and incident latency statistics.

        Returns:
            Dictionary containing:
                total_incidents (int): Total recorded incident count.
                p50_latency_ms (float): 50th percentile detection latency.
                p95_latency_ms (float): 95th percentile detection latency.
                p99_latency_ms (float): 99th percentile detection latency.
                avg_dwell_seconds (float): Average dwell duration in seconds.
                total_today (int): Incidents occurred in the last 24 hours.
        """
        with self._lock:
            cursor = self._conn.cursor()

            # Total incidents
            cursor.execute("SELECT COUNT(*) FROM security_incidents")
            total_incidents = cursor.fetchone()[0] or 0

            # Total incidents in the last 24 hours
            cursor.execute(
                """
                SELECT COUNT(*) FROM security_incidents
                WHERE timestamp >= datetime('now', '-24 hours')
                """
            )
            total_today = cursor.fetchone()[0] or 0

            # Average dwell duration
            cursor.execute(
                """
                SELECT AVG(dwell_duration_seconds) FROM security_incidents
                WHERE dwell_duration_seconds IS NOT NULL
                """
            )
            avg_dwell_res = cursor.fetchone()[0]
            avg_dwell_seconds = round(float(avg_dwell_res), 2) if avg_dwell_res is not None else 0.0

            # Latency percentiles
            cursor.execute(
                """
                SELECT detection_latency_ms FROM security_incidents
                WHERE detection_latency_ms IS NOT NULL
                ORDER BY detection_latency_ms ASC
                """
            )
            latencies = [float(row[0]) for row in cursor.fetchall()]

            p50_latency_ms = _calculate_percentile(latencies, 50.0)
            p95_latency_ms = _calculate_percentile(latencies, 95.0)
            p99_latency_ms = _calculate_percentile(latencies, 99.0)

            return {
                "total_incidents": total_incidents,
                "p50_latency_ms": p50_latency_ms,
                "p95_latency_ms": p95_latency_ms,
                "p99_latency_ms": p99_latency_ms,
                "avg_dwell_seconds": avg_dwell_seconds,
                "total_today": total_today,
            }

    def get_incident_by_id(self, incident_id: int) -> dict | None:
        """Lookup a single incident by its unique ID.

        Args:
            incident_id: Primary key ID of the incident.

        Returns:
            Dictionary representation of the incident row, or None if not found.
        """
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT * FROM security_incidents WHERE incident_id = ?",
                (incident_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    def update_classification(
        self,
        incident_id: int,
        new_classification: str,
        new_subject_name: str | None = None,
    ) -> bool:
        """Update the classification and optionally subject_name of an incident.

        Args:
            incident_id: Primary key ID of the target incident.
            new_classification: Updated classification category.
            new_subject_name: Optional updated subject name.

        Returns:
            True if a matching row was updated, False otherwise.
        """
        with self._lock:
            cursor = self._conn.cursor()
            if new_subject_name is not None:
                cursor.execute(
                    """
                    UPDATE security_incidents
                    SET classification = ?, subject_name = ?
                    WHERE incident_id = ?
                    """,
                    (new_classification, new_subject_name, incident_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE security_incidents
                    SET classification = ?
                    WHERE incident_id = ?
                    """,
                    (new_classification, incident_id),
                )
            self._conn.commit()
            updated = cursor.rowcount > 0
            if updated:
                logger.info("Updated incident #%d to classification=%s", incident_id, new_classification)
            return updated

    def close(self) -> None:
        """Close the SQLite database connection."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                    logger.debug("Closed TelemetryDB connection.")
                except sqlite3.Error as exc:
                    logger.error("Error closing SQLite connection: %s", exc)

    def __enter__(self) -> "TelemetryDB":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# Module-level singleton
telemetry_db = TelemetryDB()
