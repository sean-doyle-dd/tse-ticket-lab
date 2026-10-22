"""Alembic environment: uses the app's own config and models as the source of truth."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import config as app_config
from app.extensions import db
from app import models  # noqa: F401  (registers tables on db.metadata)

cfg = context.config
if cfg.config_file_name is not None:
    fileConfig(cfg.config_file_name)
cfg.set_main_option("sqlalchemy.url", app_config.load()["SQLALCHEMY_DATABASE_URI"].replace("%", "%%"))
target_metadata = db.metadata


def run_offline():
    context.configure(url=cfg.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online():
    connectable = engine_from_config(cfg.get_section(cfg.config_ini_section, {}), prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
