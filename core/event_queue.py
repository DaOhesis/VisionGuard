"""
CovertOps - Thread-Safe In-Memory Event Queue

This module provides a thread-safe, non-blocking in-memory event bus decoupling
the real-time vision/camera hot loop (producer) from async background cold path
tasks such as snapshot disk persistence, telemetry SQLite logging, and remote
notifications (consumer).

Incident Payload Schema Reference:
---------------------------------
incident_payload = {
    "timestamp": float,        # time.time()
    "threat_type": str,        # 'LEAD_SUPERVISOR', 'COLLEAGUE', 'UNKNOWN'
    "reaction_latency_ms": float,
    "frame_crop": np.ndarray,  # cropped visitor face BGR image
    "subject_name": str | None,
    "similarity_score": float | None,
    "proximity_ratio": float | None,
}
"""

import queue
import logging
from typing import Optional

logger = logging.getLogger("covertops.event_queue")


class _CallableInt(int):
    """Integer subclass supporting zero-argument invocation.

    Enables both property-style access (`bus.size`) and method-call style
    (`bus.size()`) transparently.
    """

    def __call__(self) -> int:
        return int(self)


class EventBus:
    """Thread-safe, non-blocking in-memory event bus.

    Decouples the high-frequency camera processing loop from downstream
    asynchronous consumers (telemetry, disk persistence, alerting).

    Payload Schema:
        incident_payload = {
            "timestamp": float,        # time.time()
            "threat_type": str,        # 'LEAD_SUPERVISOR', 'COLLEAGUE', 'UNKNOWN'
            "reaction_latency_ms": float,
            "frame_crop": np.ndarray,  # cropped visitor face BGR image
            "subject_name": str | None,
            "similarity_score": float | None,
            "proximity_ratio": float | None,
        }
    """

    def __init__(self, maxsize: int = 256) -> None:
        """Initialize the event queue.

        Args:
            maxsize: Maximum queue capacity before items are dropped.
                     Defaults to 256.
        """
        self._maxsize: int = maxsize
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)

    @property
    def maxsize(self) -> int:
        """Return the maximum allowed capacity of the queue."""
        return self._maxsize

    @property
    def size(self) -> int:
        """Return the current approximate number of items in the queue.

        Can be accessed as an integer property (`event_bus.size`) or called as
        a function (`event_bus.size()`).
        """
        return _CallableInt(self._queue.qsize())

    def qsize(self) -> int:
        """Return the approximate number of items in the queue (standard Queue alias)."""
        return self._queue.qsize()

    def empty(self) -> bool:
        """Return True if the queue is empty, False otherwise."""
        return self._queue.empty()

    def full(self) -> bool:
        """Return True if the queue is full, False otherwise."""
        return self._queue.full()

    def publish(self, payload: dict) -> bool:
        """Publish an incident payload to the event queue without blocking.

        Uses non-blocking insertion (`put_nowait`) to guarantee that the camera
        hot loop is never stalled by slow I/O or background worker lag. If the
        queue is saturated, the incoming payload is dropped and a warning is logged.

        Args:
            payload: Incident payload dictionary containing incident metadata.

        Returns:
            bool: True if the payload was successfully enqueued; False if dropped
                  due to queue saturation or failure.
        """
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            threat_type = (
                payload.get("threat_type", "UNKNOWN")
                if isinstance(payload, dict)
                else "INVALID"
            )
            logger.warning(
                "Event bus buffer saturated (max size=%d). Dropped incident event (threat_type=%s).",
                self._maxsize,
                threat_type,
            )
            return False
        except Exception as exc:
            logger.error(
                "Unexpected error publishing incident to event bus: %s",
                exc,
                exc_info=True,
            )
            return False

    def consume(self, timeout: float = 1.0) -> Optional[dict]:
        """Consume the next incident payload from the queue, blocking up to timeout.

        Intended for invocation within background workers or telemetry dispatch loops.

        Args:
            timeout: Maximum time in seconds to block waiting for an event.
                     Defaults to 1.0 second.

        Returns:
            Optional[dict]: The incident payload dictionary if available within
                            the timeout window, or None if timed out / empty.
        """
        try:
            item = self._queue.get(block=True, timeout=timeout)
            try:
                self._queue.task_done()
            except ValueError:
                pass
            return item
        except queue.Empty:
            return None
        except Exception as exc:
            logger.error(
                "Unexpected error consuming incident from event bus: %s",
                exc,
                exc_info=True,
            )
            return None

    def drain(self) -> list[dict]:
        """Flush and return all pending items currently buffered in the queue.

        Typically invoked during system shutdown or pipeline teardown to guarantee
        orderly persistence of remaining events.

        Returns:
            list[dict]: List of all drained payload dictionaries in FIFO order.
        """
        items: list[dict] = []
        while True:
            try:
                item = self._queue.get_nowait()
                items.append(item)
                try:
                    self._queue.task_done()
                except ValueError:
                    pass
            except queue.Empty:
                break
            except Exception as exc:
                logger.error(
                    "Unexpected error draining event bus: %s",
                    exc,
                    exc_info=True,
                )
                break
        return items


# Singleton-style module-level instance
event_bus = EventBus()

__all__ = ["EventBus", "event_bus"]
