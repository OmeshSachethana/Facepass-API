from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from facepass.core.security import utcnow
from facepass.db.models import RateLimitCounter


@dataclass(frozen=True)
class RateLimitStatus:
    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int


def _window_start(now: datetime, window_seconds: int) -> datetime:
    return datetime.fromtimestamp(now.timestamp() // window_seconds * window_seconds, UTC)


def _status(count: int, limit: int, start: datetime, now: datetime, window: int) -> RateLimitStatus:
    reset_after = max(1, int(start.timestamp() + window - now.timestamp()))
    return RateLimitStatus(count <= limit, limit, max(0, limit - count), reset_after)


def hit(db: Session, key: str, limit: int, window_seconds: int) -> RateLimitStatus:
    """Count one event. The upsert is atomic, so concurrent replicas can't lose increments."""
    now = utcnow()
    start = _window_start(now, window_seconds)
    statement = (
        insert(RateLimitCounter)
        .values(key=key, window_start=start, window_seconds=window_seconds, count=1)
        .on_conflict_do_update(
            index_elements=["key", "window_start"], set_={"count": RateLimitCounter.count + 1}
        )
        .returning(RateLimitCounter.count)
    )
    count = db.execute(statement).scalar_one()
    return _status(count, limit, start, now, window_seconds)


def peek(db: Session, key: str, limit: int, window_seconds: int) -> RateLimitStatus:
    """Read a counter without changing it. The limit is reached once `count` equals `limit`."""
    now = utcnow()
    start = _window_start(now, window_seconds)
    count = db.scalar(
        select(RateLimitCounter.count).where(
            RateLimitCounter.key == key, RateLimitCounter.window_start == start
        )
    )
    return _status((count or 0) + 1, limit, start, now, window_seconds)


def reset(db: Session, key: str) -> None:
    db.execute(delete(RateLimitCounter).where(RateLimitCounter.key == key))
