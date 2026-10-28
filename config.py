"""
VisionGuard - Central System Configuration and Runtime Thresholds
"""

import os
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
KNOWN_FACES_DIR = DATA_DIR / "known_faces"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
DB_PATH = DATA_DIR / "telemetry.db"

# Camera Settings
CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
TARGET_FPS = 30

# Detection & Proximity Thresholds
FACE_DETECTION_CONFIDENCE = 0.5
SECONDARY_PROXIMITY_RATIO_TRIGGER = 0.035  # Trigger when secondary face area / frame area > 3.5%
APPROACH_VELOCITY_TRIGGER = 0.015         # Trigger on rapid scaling delta

# Classification & Threat Matching
COSINE_SIMILARITY_THRESHOLD = 0.65         # Embedding match threshold for known faces

# Timing & Restoration Settings
COAST_IS_CLEAR_COOLDOWN_SEC = 4.0          # Seconds secondary face must be absent before auto-restore
DECOY_STARTUP_GRACE_PERIOD_SEC = 0.5

# Decoy & Actuation Settings
DECOY_TYPE = "TERMINAL_SIM"               # Options: 'TERMINAL_SIM', 'VS_CODE', 'BROWSER'
MUTE_AUDIO_ON_TRIGGER = True
RESTORE_AUDIO_ON_CLEAR = True

# Backend API Settings
API_HOST = "127.0.0.1"
API_PORT = 8000
