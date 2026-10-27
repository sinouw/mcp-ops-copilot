"""Command line: `opscopilot {serve,seed,run,eval}`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from opscopilot.config import Settings, get_settings
from opscopilot.db.pool import Database
from opscopilot.db.seed import reset_and_seed


def _settings(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    if getattr(args, "embedded_db", False):
        settings = settings.model_copy(update={"database_url": embedded_database_url()})
        os.environ["DATABASE_URL"] = settings.database_url
    if getattr(args, "provider", None):
        settings = settings.model_copy(update={"llm_provider": args.provider})
    if getattr(args, "model", None):
        settings = settings.model_copy(update={"llm_model": args.model})
    return settings


def embedded_database_url(data_dir: str = ".pgdata") -> str:
    """Start (or reuse) a local Postgres via pgserver, for machines without Docker."""
    import pgserver  # dev dependency

    server = pgserver.get_server(str(Path(data_dir).resolve()), cleanup_mode=None)
    uri: str = server.get_uri()
    return uri


async def _seed(settings: Settings) -> None:
    db = Database(settings.database_url)
    await reset_and_seed(db)
    await db.close()
    print("database reset and seeded")


async def _run(settings: Settings, ticket_id: str, auto_approve: bool) -> None:
    from opscopilot.llm.factory import create_llm
    from opscopilot.runtime.bootstrap import build_gateway
    from opscopilot.runtime.service import RunService

    db = Database(settings.database_url)
    async with build_gateway(settings) as gateway:
        service = RunService(db, gateway, create_llm(settings), settings)
        run_id = await service.start(ticket_id)
        while auto_approve:
            pending = await db.fetch(
                "SELECT id, tool FROM approvals WHERE run_id = $1::uuid AND status = 'pending'",
                run_id,
            )
            if not pending:
                break
            for ap in pending:
                print(f"auto-approving {ap['tool']}")
                await service.decide(str(ap["id"]), approve=True, reviewer="cli", note=None)
            await service.advance(run_id)
        row = await db.fetchrow("SELECT status, outcome FROM runs WHERE id = $1::uuid", run_id)
    await db.close()
    print(json.dumps({"run_id": run_id, **(row or {})}, indent=2, default=str))


async def _eval(settings: Settings, scenarios: Path, out: Path, only: list[str]) -> None:
    from opscopilot.evals import load_scenarios, run_suite
    from opscopilot.llm.factory import create_llm
    from opscopilot.runtime.bootstrap import build_gateway

    db = Database(settings.database_url)
    selected = [s for s in load_scenarios(scenarios) if not only or s.id in only]
    llm = create_llm(settings)
    print(f"running {len(selected)} scenarios with {llm.provider}/{llm.model}")
    async with build_gateway(settings) as gateway:
        summary = await run_suite(selected, db, gateway, llm, settings, out)
    await db.close()
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="opscopilot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "seed", "run", "eval"):
        p = sub.add_parser(name)
        p.add_argument("--embedded-db", action="store_true", help="use a local pgserver Postgres")
        if name in ("run", "eval"):
            p.add_argument("--provider")
            p.add_argument("--model")
        if name == "run":
            p.add_argument("ticket_id")
            p.add_argument("--auto-approve", action="store_true")
        if name == "eval":
            p.add_argument("--scenarios", type=Path, default=Path("evals/scenarios.yaml"))
            p.add_argument("--out", type=Path, default=Path("evals/results"))
            p.add_argument("--only", nargs="*", default=[])
        if name == "serve":
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = _settings(args)

    if args.cmd == "seed":
        asyncio.run(_seed(settings))
    elif args.cmd == "run":
        asyncio.run(_run(settings, args.ticket_id, args.auto_approve))
    elif args.cmd == "eval":
        asyncio.run(_eval(settings, args.scenarios, args.out, args.only))
    elif args.cmd == "serve":
        import uvicorn

        from opscopilot.api.app import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
