"""Races forced with a barrier, so parallel requests really overlap inside the handler."""

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from facepass.core.config import Settings
from facepass.db.models import FaceTemplate
from facepass.engine.base import FaceAnalysis, ImageArray
from facepass.main import create_app
from tests.conftest import Tenant
from tests.fakes import FakeFaceEngine, person, similar_person
from tests.helpers import create_subject, enroll, enrolled_subject, upload


class BarrierEngine(FakeFaceEngine):
    """Holds each analysis until all parallel requests are inside inference."""

    def __init__(self) -> None:
        self.barrier: threading.Barrier | None = None

    def analyze(self, image: ImageArray, *, check_liveness: bool) -> FaceAnalysis:
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        return super().analyze(image, check_liveness=check_liveness)


@pytest.fixture
def engine() -> BarrierEngine:
    return BarrierEngine()


@pytest.fixture
def race_client(
    settings: Settings, session_factory: sessionmaker[Session], engine: BarrierEngine
) -> Iterator[TestClient]:
    with TestClient(create_app(settings, engine)) as client:
        yield client


def _in_parallel(*calls: Callable[[], httpx.Response]) -> list[httpx.Response]:
    with ThreadPoolExecutor(len(calls)) as pool:
        return [future.result() for future in [pool.submit(call) for call in calls]]


def test_parallel_uploads_produce_one_outcome(
    race_client: TestClient, engine: BarrierEngine, tenant: Tenant, db: Session
) -> None:
    subject_id = create_subject(race_client, tenant)
    created = race_client.post(
        "/v1/sessions",
        json={"action": "enroll", "subject_id": subject_id, "consent": True},
        headers=tenant.headers,
    ).json()

    def submit() -> httpx.Response:
        response: httpx.Response = race_client.post(
            "/v1/sessions/submit",
            files=upload(person(1)),
            headers={"Authorization": f"Bearer {created['token']}"},
        )
        return response

    engine.barrier = threading.Barrier(2)
    responses = _in_parallel(submit, submit)

    assert sorted(response.status_code for response in responses) == [202, 409]
    # The losing request's enrollment was rolled back along with its session update.
    assert db.scalar(select(func.count(FaceTemplate.id))) == 1


def test_parallel_enrollments_respect_face_limit(
    race_client: TestClient, engine: BarrierEngine, tenant: Tenant, db: Session
) -> None:
    subject_id = enrolled_subject(race_client, tenant, person(1))

    def add() -> httpx.Response:
        response: httpx.Response = enroll(race_client, tenant, subject_id, similar_person(1))
        return response

    engine.barrier = threading.Barrier(2)
    responses = _in_parallel(add, add)

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert db.scalar(select(func.count(FaceTemplate.id))) == 2


def test_parallel_requests_share_one_rate_limit_counter(
    settings: Settings, session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    limited = settings.model_copy(update={"rate_limit_per_minute": 5})
    with TestClient(create_app(limited, FakeFaceEngine())) as client:

        def call() -> httpx.Response:
            response: httpx.Response = client.get("/v1/subjects", headers=tenant.headers)
            return response

        responses = _in_parallel(*[call] * 20)

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 5
    assert statuses.count(429) == 15
