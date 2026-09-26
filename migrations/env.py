from logging.config import fileConfig

from alembic import context
from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.models import Base

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

SCHEMA = get_settings().db_schema


def include_name(name, type_, parent_names):
    # La BD es compartida: solo nos interesa nuestro esquema.
    if type_ == "schema":
        return name == SCHEMA
    return True


def run_migrations_online() -> None:
    with engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema=SCHEMA,
            include_schemas=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
