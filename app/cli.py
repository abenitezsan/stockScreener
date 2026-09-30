"""Línea de comandos: python -m app.cli --help"""

import argparse
import logging
import sqlite3
import sys
import time

from sqlalchemy import func, select

from app import jobs, universe
from app.db import SessionLocal, engine
from app.logging_setup import setup_logging
from app.models import (
    FinancialsAnnual,
    Fundamentals,
    PriceHistory,
    Quote,
    Security,
    Valuation,
)

TASKS = {
    "fx": jobs.refresh_fx,
    "profiles": jobs.refresh_profiles,
    "history": jobs.refresh_history,
    "quotes": jobs.refresh_quotes,
    "financials": jobs.refresh_financials,
    "valuations": jobs.recompute_valuations,
}
# Orden de la carga inicial completa
BOOTSTRAP = ["fx", "profiles", "fx", "history", "quotes", "financials", "valuations"]


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("universe", help="Descarga los índices y actualiza el universo")
    p.add_argument("--source", nargs="*", choices=list(universe.SOURCES))
    p.add_argument(
        "--file",
        help="Leer la fuente de un fichero local en lugar de descargarla (p. ej. el CSV de "
        "iShares bajado desde el navegador); requiere una sola --source",
    )

    p = sub.add_parser("import-csv", help="Añade valores desde un CSV (columna symbol)")
    p.add_argument("path")
    p.add_argument("--universe", default=universe.MANUAL)

    p = sub.add_parser("add", help="Añade valores a mano (ticker de Yahoo: SAN.MC, ENB.TO…)")
    p.add_argument("symbols", nargs="+")

    p = sub.add_parser("refresh", help="Ejecuta tareas de actualización")
    p.add_argument("tasks", nargs="+", choices=[*TASKS, "bootstrap"])
    p.add_argument("--symbols", nargs="*", help="Limitar a estos valores")
    p.add_argument(
        "--missing",
        action="store_true",
        help="Solo los valores activos que aún no tienen ficha (p. ej. recién añadidos)",
    )

    sub.add_parser(
        "full-load",
        help="Carga completa: descarga todos los índices y todos los datos (~1-2 h). "
        "Lánzala en segundo plano y sigue el progreso en logs/cli.log",
    )
    sub.add_parser("status", help="Resumen de lo que hay en la base de datos")

    p = sub.add_parser("backup", help="Copia consistente de la base de datos (aunque esté en uso)")
    p.add_argument("path", help="Fichero de destino, p. ej. /data/backup-2026-09-27.db")

    args = parser.parse_args()
    setup_logging("cli.log")
    logging.getLogger(__name__).info("CLI: %s", " ".join(sys.argv[1:]))

    if args.command == "backup":
        backup(args.path)
        return

    with SessionLocal() as session:
        if args.command == "full-load":
            full_load(session)
        elif args.command == "status":
            print_status(session)
        elif args.command == "universe":
            for name, result in universe.sync_sources(session, args.source, args.file).items():
                print(f"{name}: {result}")
        elif args.command == "import-csv":
            print(universe.import_csv(session, args.path, args.universe), "valores")
        elif args.command == "add":
            print(universe.upsert_universe(session, universe.MANUAL, args.symbols), "valores")
        elif args.command == "refresh":
            if args.missing:
                args.symbols = list(
                    session.scalars(
                        select(Security.symbol).where(
                            Security.active,
                            Security.id.not_in(select(Fundamentals.security_id)),
                        )
                    )
                )
                print(f"Valores sin datos: {len(args.symbols)}")
                logging.getLogger("app.cli").info("Valores sin datos: %d", len(args.symbols))
                if not args.symbols:
                    return
            if args.symbols:
                args.symbols = [sym.strip().upper() for sym in args.symbols]
                known = set(
                    session.scalars(
                        select(Security.symbol).where(Security.symbol.in_(args.symbols))
                    )
                )
                if missing := sorted(set(args.symbols) - known):
                    print(
                        f"No están en el universo: {', '.join(missing)}. "
                        f"Añádelos antes con: python -m app.cli add {' '.join(missing)}"
                    )
                    if not known:
                        sys.exit(1)
            names = BOOTSTRAP if args.tasks == ["bootstrap"] else args.tasks
            for name in names:
                task = TASKS[name]
                kwargs = {"symbols": args.symbols} if args.symbols and name != "fx" else {}
                print(f"{name}: {task(session, **kwargs)}")


def full_load(session) -> None:
    """Universo + bootstrap. Informa por el log (con `exec -d` la salida estándar se pierde)."""
    log = logging.getLogger("app.cli")
    start = time.monotonic()
    log.info("=== Carga completa: 1/2 universo ===")
    for name, result in universe.sync_sources(session).items():
        log.info("universo %s: %s", name, result)
    log.info("=== Carga completa: 2/2 datos de mercado ===")
    for name in BOOTSTRAP:
        log.info("%s: %s", name, TASKS[name](session))
    log.info("=== Carga completa terminada en %d min ===", (time.monotonic() - start) / 60)
    for line in status_lines(session):
        log.info(line)


def status_lines(session) -> list[str]:
    def count(query):
        return session.scalar(query) or 0

    active = count(select(func.count()).select_from(Security).where(Security.active))
    with_fair_value = (
        select(func.count()).select_from(Valuation).where(Valuation.fair_value.is_not(None))
    )
    lines = [
        f"valores activos: {active}",
        f"con ficha: {count(select(func.count()).select_from(Fundamentals))}",
        f"con histórico: {count(select(func.count(func.distinct(PriceHistory.security_id))))}",
        f"con cotización: {count(select(func.count()).select_from(Quote))}",
        f"con cuentas: {count(select(func.count(func.distinct(FinancialsAnnual.security_id))))}",
        f"valorados: {count(select(func.count()).select_from(Valuation))}",
        f"con precio justo: {count(with_fair_value)}",
    ]
    by_universe: dict[str, int] = {}
    for tags in session.scalars(select(Security.universes).where(Security.active)):
        for tag in tags or []:
            by_universe[tag] = by_universe.get(tag, 0) + 1
    lines.append("por índice: " + ", ".join(f"{k} {v}" for k, v in sorted(by_universe.items())))
    return lines


def print_status(session) -> None:
    for line in status_lines(session):
        print(line)


def backup(path: str) -> None:
    """Usa la API de backup de SQLite: copiar el fichero a mano con WAL activo no es seguro."""
    source = sqlite3.connect(engine.url.database)
    target = sqlite3.connect(path)
    with target:
        source.backup(target)
    target.close()
    source.close()
    print(f"Copia guardada en {path}")


if __name__ == "__main__":
    main()
