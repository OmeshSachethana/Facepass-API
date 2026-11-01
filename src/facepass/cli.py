"""Operator commands. Printed secrets exist only in this output, so store them right away."""

import argparse
import uuid
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from facepass.core.config import Settings, get_settings
from facepass.core.security import (
    encrypt_secret,
    generate_api_key,
    generate_webhook_secret,
    hash_token,
    utcnow,
)
from facepass.db.models import ApiKey, Application
from facepass.db.session import build_engine, build_session_factory
from facepass.engine.manifest import download_models


def _open_session() -> Session:
    return build_session_factory(build_engine(get_settings().database_url))()


def _validate_webhook_url(url: str, settings: Settings) -> str:
    parsed = urlparse(url)
    allowed = {"https"} if settings.environment == "production" else {"https", "http"}
    if parsed.scheme not in allowed or not parsed.hostname:
        raise SystemExit(f"Webhook URL must use one of: {', '.join(sorted(allowed))}")
    return url


def _set_webhook(application: Application, url: str, settings: Settings) -> str:
    secret = generate_webhook_secret()
    application.webhook_url = _validate_webhook_url(url, settings)
    application.webhook_secret_encrypted = encrypt_secret(
        secret, settings.secret_key.get_secret_value()
    )
    return secret


def _add_key(db: Session, application_id: uuid.UUID, name: str) -> tuple[uuid.UUID, str]:
    key = generate_api_key()
    key_id = uuid.uuid4()
    db.add(ApiKey(id=key_id, application_id=application_id, name=name, key_hash=hash_token(key)))
    return key_id, key


def _apps_create(args: argparse.Namespace) -> None:
    with _open_session() as db:
        application = Application(id=uuid.uuid4(), name=args.name)
        secret = None
        if args.webhook_url:
            secret = _set_webhook(application, args.webhook_url, get_settings())
        db.add(application)
        db.flush()
        key_id, key = _add_key(db, application.id, "default")
        db.commit()
        print(f"application_id: {application.id}")
        print(f"api_key_id:     {key_id}")
        print(f"api_key:        {key}")
        if secret:
            print(f"webhook_secret: {secret}")


def _apps_list(_: argparse.Namespace) -> None:
    with _open_session() as db:
        for application in db.scalars(select(Application).order_by(Application.created_at)):
            state = "disabled" if application.disabled_at else "active"
            print(f"{application.id}  {state:8}  {application.name}")


def _apps_set_webhook(args: argparse.Namespace) -> None:
    with _open_session() as db:
        application = db.get(Application, args.application_id)
        if application is None:
            raise SystemExit("Application not found")
        secret = _set_webhook(application, args.url, get_settings())
        db.commit()
        print(f"webhook_secret: {secret}")


def _keys_create(args: argparse.Namespace) -> None:
    with _open_session() as db:
        if db.get(Application, args.application_id) is None:
            raise SystemExit("Application not found")
        key_id, key = _add_key(db, args.application_id, args.name)
        db.commit()
        print(f"api_key_id: {key_id}")
        print(f"api_key:    {key}")


def _keys_revoke(args: argparse.Namespace) -> None:
    with _open_session() as db:
        api_key = db.get(ApiKey, args.key_id)
        if api_key is None:
            raise SystemExit("API key not found")
        api_key.revoked_at = utcnow()
        db.commit()
        print("Revoked")


def _models_download(args: argparse.Namespace) -> None:
    # Runs during image builds, so it must not need database settings.
    download_models(Path(args.directory))
    print(f"Models ready in {args.directory}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="facepass", description=__doc__)
    groups = parser.add_subparsers(dest="group", required=True)

    apps = groups.add_parser("apps", help="Manage applications").add_subparsers(required=True)
    create = apps.add_parser("create", help="Create an application and its first API key")
    create.add_argument("--name", required=True)
    create.add_argument("--webhook-url")
    create.set_defaults(handler=_apps_create)
    apps.add_parser("list", help="List applications").set_defaults(handler=_apps_list)
    set_webhook = apps.add_parser("set-webhook", help="Set the URL and rotate the signing secret")
    set_webhook.add_argument("application_id", type=uuid.UUID)
    set_webhook.add_argument("url")
    set_webhook.set_defaults(handler=_apps_set_webhook)

    keys = groups.add_parser("keys", help="Manage API keys").add_subparsers(required=True)
    key_create = keys.add_parser("create", help="Create an API key")
    key_create.add_argument("application_id", type=uuid.UUID)
    key_create.add_argument("--name", default="default")
    key_create.set_defaults(handler=_keys_create)
    key_revoke = keys.add_parser("revoke", help="Revoke an API key")
    key_revoke.add_argument("key_id", type=uuid.UUID)
    key_revoke.set_defaults(handler=_keys_revoke)

    models = groups.add_parser("models", help="Manage model files").add_subparsers(required=True)
    download = models.add_parser("download", help="Download and verify the model files")
    download.add_argument("--directory", default="models")
    download.set_defaults(handler=_models_download)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
