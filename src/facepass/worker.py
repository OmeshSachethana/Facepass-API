import logging
import signal
import threading
from types import FrameType

import httpx

from facepass.core.config import get_settings
from facepass.core.logging import configure_logging
from facepass.db.session import build_engine, build_session_factory
from facepass.services import maintenance, webhooks

logger = logging.getLogger(__name__)

_MAINTENANCE_INTERVAL_POLLS = 300
_MAINTENANCE_LOCK_ID = 7_315_001


def main() -> None:
    """Deliver webhooks, close expired sessions and enforce retention until told to stop."""
    settings = get_settings()
    configure_logging(settings.log_level)
    session_factory = build_session_factory(build_engine(settings.database_url))
    stopping = threading.Event()

    def stop(_signal: int, _frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info("Worker started")
    # follow_redirects stays off: a signed payload must only go to the configured URL.
    with httpx.Client(timeout=settings.webhook_timeout_seconds) as client:
        polls = 0
        while not stopping.is_set():
            try:
                maintenance.expire_sessions(session_factory)
                sent = webhooks.deliver_due(session_factory, client, settings)
                if polls % _MAINTENANCE_INTERVAL_POLLS == 0:
                    with session_factory() as db:
                        if maintenance.try_lock(db, _MAINTENANCE_LOCK_ID):
                            maintenance.purge_old_records(session_factory, settings)
            except Exception:
                # A database outage must not kill the worker. It retries on the next poll.
                logger.exception("Worker iteration failed")
                sent = 0
            polls += 1
            if not sent:
                stopping.wait(settings.worker_poll_seconds)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
