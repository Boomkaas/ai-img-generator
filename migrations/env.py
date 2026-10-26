from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from common.db import normalise_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url() -> str:
    # Tests pass the URL explicitly; otherwise use DATABASE_URL from the settings.
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        from common.config import get_settings

        url = get_settings().DATABASE_URL
    return normalise_url(url)


def run_migrations_offline() -> None:
    context.configure(url=database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
