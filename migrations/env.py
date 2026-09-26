from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

from app.config import get_settings
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
    # search_path fijo: si el usuario se llama igual que el esquema, Postgres lo tomaría como
    # esquema por defecto ("$user") y la comparación de autogenerate/check dejaría de verlo.
    engine = create_engine(
        get_settings().database_url, connect_args={"options": "-c search_path=public"}
    )
    with engine.connect() as connection:
        # Solo si falta: un usuario dueño de su esquema no suele tener permiso CREATE en la BD,
        # y Postgres lo exige incluso con IF NOT EXISTS.
        exists = connection.scalar(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": SCHEMA}
        )
        if not exists:
            connection.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
        # Cerrar la transacción implícita: si no, Alembic la reutiliza y no hace commit
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
    engine.dispose()


run_migrations_online()
