"""
CovertOps ("Academic Shield") - Central System Orchestrator & Hot-Loop Runtime

This module is the master execution entrypoint for the CovertOps workstation
defense system. It orchestrates the high-frequency Hot Path (frame ingestion,
face detection, operator filtering, threat matching, and sub-2ms OS actuation)
and decouples persistence into the Cold Path (non-blocking in-memory event bus
and background worker).

Usage:
    python main.py                  # Standard execution with live optical sensor
    python main.py --mock           # Simulated loop (runs without physical camera)
    python main.py --headless       # Run without OpenCV GUI display window
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

# Ensure root directory is in sys.path
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from core.event_queue import event_bus
from background.database import telemetry_db
from background.worker import background_worker

# Configure system-wide logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("covertops.orchestrator")


# --- State Machine Tiers ---
class DefenseState:
    NORMAL = "NORMAL"                     # No secondary face detected
    TIER_1_COLLEAGUE = "COLLEAGUE"        # Whitelisted peer in range (HUD alert)
    TIER_2_UNKNOWN = "UNKNOWN"            # Unverified person in range (pre-cache)
    DEFCON_1_THREAT = "LEAD_SUPERVISOR"   # Supervisory VIP in range (Atomic camouflage)
    COASTING_CLEAR = "COASTING_CLEAR"     # Subject departed; cooldown countdown


class CovertOpsOrchestrator:
    """Master orchestrator managing lifecycle, state machine, and the real-time hot loop."""

    def __init__(self, mock_mode: bool = False, headless: bool = False) -> None:
        self.mock_mode = mock_mode
        self.headless = headless
        self._running = False

        # State tracking
        self.current_state = DefenseState.NORMAL
        self.active_incident = False
        self.threat_departed_time: Optional[float] = None
        self.pre_event_active_window = None

        # Performance metrics
        self.frame_count = 0
        self.start_time = time.time()
        self.last_diagnostics_time = time.time()
        self.loop_fps = 0.0

        # Subsystems (lazy initialized or stubbed for modular independence)
        self.camera = None
        self.detector = None
        self.tracker = None
        self.matcher = None
        self.os_actions = None

    def initialize_subsystems(self) -> None:
        """Dynamically load Member 1 (Vision) and Member 2 (Matching/Actuation) modules with safe fallbacks."""
        logger.info("Initializing CovertOps subsystem modules...")

        # 1. Camera / Ingestion (Member 1)
        try:
            from core.camera import ThreadedCamera
            self.camera = ThreadedCamera(
                camera_index=config.CAMERA_INDEX,
                width=config.CAMERA_WIDTH,
                height=config.CAMERA_HEIGHT,
            )
            logger.info("Loaded native ThreadedCamera ingestion.")
        except ImportError:
            logger.warning("core.camera module not yet present. Using default capture stub.")

        # 2. Face Detector (Member 1)
        try:
            from core.detector import FaceDetector
            self.detector = FaceDetector(min_confidence=config.FACE_DETECTION_CONFIDENCE)
            logger.info("Loaded MediaPipe BlazeFace detector.")
        except ImportError:
            logger.warning("core.detector module not yet present. Using detection stub.")

        # 3. Spatial Tracker & Operator Anchoring (Member 1)
        try:
            from core.tracker import SpatialTracker
            self.tracker = SpatialTracker(
                proximity_trigger=config.SECONDARY_PROXIMITY_RATIO_TRIGGER,
                velocity_trigger=config.APPROACH_VELOCITY_TRIGGER,
            )
            logger.info("Loaded SpatialTracker operator anchoring.")
        except ImportError:
            logger.warning("core.tracker module not yet present. Using tracker stub.")

        # 4. Face Feature Matcher (Member 2)
        try:
            from core.matcher import FaceMatcher
            self.matcher = FaceMatcher(
                known_faces_dir=config.KNOWN_FACES_DIR,
                similarity_threshold=config.COSINE_SIMILARITY_THRESHOLD,
            )
            logger.info("Loaded FaceMatcher embedding comparator.")
        except ImportError:
            logger.warning("core.matcher module not yet present. Using matcher stub.")

        # 5. Native OS Actuation (Member 2)
        try:
            from core.os_actions import OSActuator
            self.os_actions = OSActuator()
            logger.info("Loaded native Win32/PyCaw OSActuator.")
        except ImportError:
            logger.warning("core.os_actions module not yet present. Using actuation stub.")

    def start(self) -> None:
        """Start the orchestrator: background telemetry workers and main hot loop."""
        logger.info("Starting Project CovertOps ('Academic Shield')...")
        self._running = True

        # Register signal handlers for graceful termination if running in main thread
        import threading
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._handle_shutdown_signal)
                signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
            except (ValueError, AttributeError):
                pass

        # 1. Start Cold Path background worker daemon
        background_worker.start()

        # 2. Initialize optional live camera stream if available and not in mock mode
        if not self.mock_mode and self.camera is not None:
            self.camera.start()

        # 3. Enter real-time Hot Loop
        logger.info("Entering real-time Hot Loop. System is ARMED and PROTECTING.")
        try:
            self._hot_loop()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received.")
        finally:
            self.shutdown()

    def _hot_loop(self) -> None:
        """Real-time hot path (< 40ms per iteration) executing detection, threat classification, and actuation."""
        last_frame_time = time.time()

        while self._running:
            loop_start = time.perf_counter()

            # --- Stage 1: Frame Acquisition ---
            if self.mock_mode:
                # Simulate 30 FPS timing and generate mock visitor periodically
                time.sleep(1.0 / config.TARGET_FPS)
                frame = np.zeros((config.CAMERA_HEIGHT, config.CAMERA_WIDTH, 3), dtype=np.uint8)
                is_mock_trigger = (self.frame_count % 90 == 45)  # Periodic test event every ~3 seconds
                detection_result = self._mock_detection(frame, trigger=is_mock_trigger)
            else:
                frame = self._acquire_frame()
                if frame is None:
                    time.sleep(0.005)
                    continue
                detection_result = self._process_frame(frame)

            # --- Stage 2 & 3: Threat Evaluation & State Machine ---
            self._evaluate_threat_state(detection_result, frame, loop_start)

            # --- Stage 4: FPS & Telemetry Tracking ---
            self.frame_count += 1
            now = time.time()
            if now - last_frame_time > 0:
                self.loop_fps = 0.9 * self.loop_fps + 0.1 * (1.0 / (now - last_frame_time))
            last_frame_time = now

            # Periodic diagnostic logging every 5 seconds
            if now - self.last_diagnostics_time >= 5.0:
                self._record_diagnostics()
                self.last_diagnostics_time = now

    def _acquire_frame(self) -> Optional[np.ndarray]:
        """Fetch latest zero-buffer frame from ThreadedCamera or standard VideoCapture."""
        if self.camera is not None:
            return self.camera.read()
        return None

    def _process_frame(self, frame: np.ndarray) -> Dict[str, Any]:
        """Run detection, spatial tracking, and facial recognition on the current frame."""
        # Member 1 + Member 2 pipeline contracts
        if self.detector is not None and self.tracker is not None:
            faces = self.detector.detect_faces(frame)
            tracking_info = self.tracker.update(faces, frame.shape)
            return tracking_info
        return {"secondary_face_detected": False}

    def _evaluate_threat_state(
        self,
        detection: Dict[str, Any],
        frame: np.ndarray,
        loop_start_time: float,
    ) -> None:
        """Evaluate spatial proximity, match identity, execute camouflage, and dispatch telemetry."""
        now = time.time()
        secondary_detected = detection.get("secondary_face_detected", False)

        if secondary_detected:
            self.threat_departed_time = None
            classification = detection.get("classification", DefenseState.TIER_2_UNKNOWN)
            subject_name = detection.get("subject_name", "Unknown Visitor")
            similarity_score = detection.get("similarity_score", 0.0)
            proximity_ratio = detection.get("proximity_ratio", 0.05)

            if not self.active_incident:
                # Calculate latency from frame ingestion to actuation
                actuation_start = time.perf_counter()

                # Execute OS Camouflage
                if classification == DefenseState.DEFCON_1_THREAT:
                    self._execute_camouflage(immediate_mute=True)
                    self.current_state = DefenseState.DEFCON_1_THREAT
                elif classification == DefenseState.TIER_2_UNKNOWN:
                    self._execute_cautious_precache()
                    self.current_state = DefenseState.TIER_2_UNKNOWN
                else:
                    self.current_state = DefenseState.TIER_1_COLLEAGUE

                reaction_latency_ms = (time.perf_counter() - loop_start_time) * 1000.0

                # Crop visitor face if bbox available, else center crop
                bbox = detection.get("secondary_bbox")
                crop = self._extract_crop(frame, bbox)

                # Offload to Cold Path non-blocking bus
                payload = {
                    "timestamp": now,
                    "threat_type": classification,
                    "subject_name": subject_name,
                    "similarity_score": similarity_score,
                    "proximity_ratio": proximity_ratio,
                    "reaction_latency_ms": round(reaction_latency_ms, 2),
                    "frame_crop": crop,
                }
                published = event_bus.publish(payload)
                if published:
                    logger.info(
                        "🛡️ DEFENSE TRIGGERED [%s] in %.2f ms | Offloaded to event bus.",
                        classification,
                        reaction_latency_ms,
                    )

                self.active_incident = True

        else:
            # Threat not detected in current frame
            if self.active_incident:
                if self.threat_departed_time is None:
                    self.threat_departed_time = now
                    self.current_state = DefenseState.COASTING_CLEAR
                    logger.info(
                        "Threat departed. Coast-is-clear countdown started (%.1fs cooldown)...",
                        config.COAST_IS_CLEAR_COOLDOWN_SEC,
                    )

                # Check if threat has remained absent beyond cooldown window
                if now - self.threat_departed_time >= config.COAST_IS_CLEAR_COOLDOWN_SEC:
                    self._restore_workspace()
                    self.active_incident = False
                    self.threat_departed_time = None
                    self.current_state = DefenseState.NORMAL
                    logger.info("✅ COAST IS CLEAR. Prior workspace and audio restored.")

    def _execute_camouflage(self, immediate_mute: bool = True) -> None:
        """Trigger instantaneous decoy swap and Core Audio mute via OSActuator."""
        if self.os_actions is not None:
            self.os_actions.bring_decoy_to_foreground(decoy_type=config.DECOY_TYPE)
            if immediate_mute and config.MUTE_AUDIO_ON_TRIGGER:
                self.os_actions.mute_endpoint()
        else:
            logger.info("[Camouflage Actuation] Swapped to decoy '%s' | Audio Muted.", config.DECOY_TYPE)

    def _execute_cautious_precache(self) -> None:
        """Pre-cache decoy and attenuate audio for Tier 2 unknown approaches."""
        if self.os_actions is not None:
            self.os_actions.precache_decoy()
            if config.MUTE_AUDIO_ON_TRIGGER:
                self.os_actions.attenuate_volume(factor=0.5)
        else:
            logger.info("[Caution Actuation] Decoy pre-cached | Volume attenuated 50%.")

    def _restore_workspace(self) -> None:
        """Dismiss decoy and restore original window focus and audio volume."""
        if self.os_actions is not None:
            self.os_actions.restore_original_state()
            if config.RESTORE_AUDIO_ON_CLEAR:
                self.os_actions.unmute_endpoint()
        else:
            logger.info("[Workspace Restored] Decoy dismissed | Audio unmuted.")

    def _extract_crop(self, frame: np.ndarray, bbox: Optional[Tuple[int, int, int, int]]) -> np.ndarray:
        """Extract cropped face bounding box or generate dummy placeholder crop."""
        h, w = frame.shape[:2]
        if bbox is not None:
            bx, by, bw, bh = bbox
            x1 = max(0, bx)
            y1 = max(0, by)
            x2 = min(w, bx + bw)
            y2 = min(h, by + bh)
            crop = frame[y1:y2, x1:x2]
            if crop.size > 0:
                return crop.copy()

        # Fallback square center crop
        min_dim = min(h, w) // 2
        cy, cx = h // 2, w // 2
        return frame[cy - min_dim // 2:cy + min_dim // 2, cx - min_dim // 2:cx + min_dim // 2].copy()

    def _mock_detection(self, frame: np.ndarray, trigger: bool) -> Dict[str, Any]:
        """Generate mock secondary approach detection for simulated runtime testing."""
        if trigger:
            return {
                "secondary_face_detected": True,
                "classification": DefenseState.DEFCON_1_THREAT,
                "subject_name": "Supervisor_Approaching (Simulation)",
                "similarity_score": 0.88,
                "proximity_ratio": 0.082,
                "secondary_bbox": (100, 80, 160, 160),
            }
        return {"secondary_face_detected": False}

    def _record_diagnostics(self) -> None:
        """Log diagnostic telemetry (FPS, CPU %, RAM MB) to SQLite."""
        try:
            import psutil
            process = psutil.Process()
            cpu_pct = process.cpu_percent()
            ram_mb = process.memory_info().rss / (1024 * 1024)
        except ImportError:
            cpu_pct = 0.0
            ram_mb = 0.0

        telemetry_db.log_diagnostics(
            loop_fps=round(self.loop_fps, 1),
            cpu_utilization_pct=round(cpu_pct, 1),
            ram_usage_mb=round(ram_mb, 1),
        )

    def _handle_shutdown_signal(self, signum: int, frame: Any) -> None:
        """Handle OS termination signals gracefully."""
        logger.info("Termination signal %s intercepted. Initiating shutdown...", signum)
        self._running = False

    def shutdown(self) -> None:
        """Cleanly terminate camera, background workers, and persistent database connections."""
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True

        logger.info("Executing Project CovertOps teardown...")
        self._running = False

        if self.camera is not None:
            try:
                self.camera.stop()
            except Exception as exc:
                logger.error("Error stopping camera: %s", exc)

        try:
            background_worker.stop(timeout=4.0)
        except Exception as exc:
            logger.error("Error stopping background worker: %s", exc)

        try:
            telemetry_db.close()
        except Exception as exc:
            logger.error("Error closing telemetry database: %s", exc)

        logger.info("Project CovertOps shutdown complete.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CovertOps - Workstation Camouflage Orchestrator")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in simulation mode without requiring a physical camera",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without displaying OpenCV window",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    orchestrator = CovertOpsOrchestrator(mock_mode=args.mock, headless=args.headless)
    orchestrator.initialize_subsystems()
    orchestrator.start()
