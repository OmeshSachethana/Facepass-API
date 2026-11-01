from alembic import command

from tests.conftest import alembic_config


def test_models_match_migrations(migrated_database: str) -> None:
    command.check(alembic_config())


def test_downgrade_and_upgrade_round_trip(migrated_database: str) -> None:
    command.downgrade(alembic_config(), "base")
    command.upgrade(alembic_config(), "head")
