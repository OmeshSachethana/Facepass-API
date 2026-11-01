from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_CONNECT_TIMEOUT_SECONDS = 10


def build_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS},
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    # Handlers commit before running inference to release their connection, then keep using
    # the loaded rows, so those rows must stay readable after a commit.
    return sessionmaker(engine, expire_on_commit=False)
