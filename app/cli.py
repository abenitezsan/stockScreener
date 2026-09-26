"""Línea de comandos: python -m app.cli --help"""

import argparse
import logging

from app import jobs, universe
from app.db import SessionLocal

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

    p = sub.add_parser("import-csv", help="Añade valores desde un CSV (columna symbol)")
    p.add_argument("path")
    p.add_argument("--universe", default=universe.MANUAL)

    p = sub.add_parser("add", help="Añade valores a mano (ticker de Yahoo: SAN.MC, ENB.TO…)")
    p.add_argument("symbols", nargs="+")

    p = sub.add_parser("refresh", help="Ejecuta tareas de actualización")
    p.add_argument("tasks", nargs="+", choices=[*TASKS, "bootstrap"])
    p.add_argument("--symbols", nargs="*", help="Limitar a estos valores")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with SessionLocal() as session:
        if args.command == "universe":
            for name, result in universe.sync_sources(session, args.source).items():
                print(f"{name}: {result}")
        elif args.command == "import-csv":
            print(universe.import_csv(session, args.path, args.universe), "valores")
        elif args.command == "add":
            print(universe.upsert_universe(session, universe.MANUAL, args.symbols), "valores")
        elif args.command == "refresh":
            names = BOOTSTRAP if args.tasks == ["bootstrap"] else args.tasks
            for name in names:
                task = TASKS[name]
                kwargs = {"symbols": args.symbols} if args.symbols and name != "fx" else {}
                print(f"{name}: {task(session, **kwargs)}")


if __name__ == "__main__":
    main()
