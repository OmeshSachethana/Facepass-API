# FacePass API

Face enrollment, verification (1:1) and identification (1:N) as an HTTP API that any web or mobile application can integrate.

- **Multi-tenant:** each application has its own keys and isolated face data.
- **Safe for clients:** web and mobile apps upload with short-lived session tokens, and results go only to your backend.
- **Stores no photos:** images are discarded after processing, and only embeddings are kept.
- **Runs on a CPU:** uses openly licensed models (YuNet, SFace and MiniFASNet).

> **Before production:** the liveness check hasn't been tested against real spoof attacks, and match thresholds have only been measured on a public benchmark. Read [docs/evaluation.md](docs/evaluation.md) and [docs/security-and-privacy.md](docs/security-and-privacy.md).

## Quick start

You need Docker.

```bash
cp .env.example .env              # then set POSTGRES_PASSWORD and FACEPASS_SECRET_KEY
docker compose up --build -d
docker compose exec api facepass apps create --name "My App"
```

The last command prints an API key once, so store it right away. Interactive API docs are at <http://localhost:8000/docs>. If ports 8000 or 5436 are taken, set `FACEPASS_API_PORT` or `FACEPASS_DB_PORT` in `.env`.

Enroll a person, then verify a new photo of them:

```bash
export KEY=fp_key_...

SUBJECT=$(curl -s localhost:8000/v1/subjects -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"external_id": "user-123"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['id'])")

curl -s localhost:8000/v1/subjects/$SUBJECT/faces -H "Authorization: Bearer $KEY" \
  -F image=@enrollment-photo.jpg -F consent=true

curl -s localhost:8000/v1/verify -H "Authorization: Bearer $KEY" \
  -F image=@new-photo.jpg -F subject_id=$SUBJECT
```

```json
{
  "match": true,
  "subject_id": "b7c4…",
  "score": 0.6712,
  "threshold": 0.4,
  "model_id": "sface-2021dec",
  "liveness": { "checked": true, "score": 0.97 }
}
```

To connect a website or mobile app, follow the [integration guide](docs/integration.md). [examples/web-demo](examples/web-demo) is a working browser example.

## Endpoints

| Method and path | Auth | Purpose |
|---|---|---|
| `POST /v1/subjects` | API key | Create a person |
| `GET /v1/subjects` | API key | List people or find one by `external_id` |
| `GET /v1/subjects/{id}` | API key | Read a person |
| `DELETE /v1/subjects/{id}` | API key | Permanently delete a person and their face data |
| `POST /v1/subjects/{id}/faces` | API key | Enroll a face |
| `GET /v1/subjects/{id}/faces` | API key | List enrolled faces |
| `DELETE /v1/subjects/{id}/faces/{face_id}` | API key | Delete one face |
| `POST /v1/verify` | API key | Check a photo against one person |
| `POST /v1/identify` | API key | Find who a photo shows |
| `POST /v1/sessions` | API key | Create a client upload session |
| `GET /v1/sessions/{id}` | API key | Read a session's outcome |
| `POST /v1/sessions/submit` | Session token | Upload a photo from a client |
| `GET /healthz`, `GET /readyz` | None | Liveness and readiness probes |

## Errors

Errors follow [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) as `application/problem+json`. Branch on `code`, and quote `request_id` when reporting a problem.

```json
{
  "type": "urn:facepass:problem:no_face_detected",
  "title": "No face detected",
  "status": 422,
  "code": "no_face_detected",
  "detail": "Center one face in the frame, facing the camera.",
  "request_id": "9f2c…"
}
```

| Code | Status | What to do |
|---|---|---|
| `no_face_detected`, `multiple_faces_detected`, `face_out_of_frame`, `face_too_small`, `face_too_blurry`, `poor_lighting` | 422 | Show `detail` and let the user retake the photo |
| `liveness_failed` | 422 | Ask for a live photo. Repeated failures lock the subject |
| `consent_required` | 422 | Collect consent, then send `consent=true` |
| `validation_error` | 422 | Fix the request. `errors` lists the fields |
| `unsupported_image`, `image_too_large`, `empty_image` | 415, 413, 400 | Send a valid JPEG, PNG or WebP. Convert iOS HEIC photos to JPEG first |
| `length_required` | 411 | Send a Content-Length header |
| `face_mismatch` | 409 | The new face doesn't match this subject's enrolled faces |
| `face_limit_reached` | 409 | Delete a face before adding another |
| `subject_not_enrolled` | 409 | Enroll a face first |
| `external_id_conflict` | 409 | Use the existing subject |
| `session_closed` | 409 | Create a new session |
| `session_expired` | 410 | Create a new session |
| `too_many_failed_attempts` | 429 | Verification is locked. Wait for `Retry-After` |
| `rate_limited` | 429 | Slow down. Wait for `Retry-After` |
| `service_busy` | 503 | Retry after `Retry-After` |

## Development

You need Python 3.12 or newer, and Docker for the test database.

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
facepass models download

docker compose -f docker-compose.test.yml up -d
pytest --cov
ruff check . && ruff format --check . && mypy
```

Run the API outside Docker with `uvicorn facepass.asgi:app --reload`, after pointing `FACEPASS_DATABASE_URL` at a database and running `alembic upgrade head`.

All settings and their defaults are in [src/facepass/core/config.py](src/facepass/core/config.py). After changing dependencies, regenerate `requirements.lock` in a Python 3.12 Linux environment:

```bash
pip-compile --strip-extras --output-file requirements.lock pyproject.toml
```

## Project layout

```text
src/facepass/
  api/        HTTP routes, auth and middleware
  services/   Enrollment, matching, sessions, webhooks, rate limits and retention
  engine/     Face models, behind the FaceEngine interface
  db/         SQLAlchemy models
  core/       Settings, errors, logging and cryptography
  cli.py      Operator commands: `facepass apps|keys|models`
  worker.py   Background worker: webhooks, session expiry and retention
migrations/   Alembic migrations
scripts/      LFW accuracy evaluation
```

## Model licenses

| Model | Purpose | License |
|---|---|---|
| [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | Face detection | MIT |
| [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) | Face embeddings | Apache-2.0 |
| [MiniFASNet](https://github.com/yakhyo/face-anti-spoofing) | Liveness | Apache-2.0 |

This repository has no project license yet. Weights are downloaded at build time and checked against the SHA-256 hashes in [manifest.py](src/facepass/engine/manifest.py).
