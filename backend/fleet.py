"""Moving the fleet in and out as YAML.

The fleet runs from the operational store (backend/store.py). YAML is how an
install that predates it comes in, and how the fleet goes out for Git, a
backup, or another instance. The layout is the old one, so the two
directions are inverses:

    fleet/hosts.yaml            hosts: [...]
    fleet/agents/<id>.yaml      one per agent
    fleet/decommissioned/       archived agents
    fleet/templates/<name>.yaml
    fleet/cron_jobs/<id>.yaml
    fleet/policy.yaml           the access policy, as written

    python -m backend.fleet import [dir]          # merge a fleet/ directory into the store
    python -m backend.fleet export [dir]          # write the store out as one

Import validates every record first and, unless told to skip bad ones, writes
nothing when any is invalid. It merges: a record with the same id is replaced,
nothing else is touched. A fresh store imports `fleet/` by itself, once, on the
first start (bootstrap); later starts never read it, so a stale directory
cannot put old records back.
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel

from . import cron_store, opstore, policy, store, templates
from .schemas import Agent, CronJob, Host

logger = logging.getLogger(__name__)

BOOTSTRAPPED = "bootstrapped"


def _read(path: Path):
    return yaml.safe_load(path.read_text())


def collect(src: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """kind -> id -> text to store, and what is wrong in `src`."""
    found: dict[str, dict[str, str]] = {k: {} for k in ("host", "agent", "decommissioned", "template", "cron", "policy")}
    errors: list[str] = []

    def attempt(label: str, build) -> None:
        try:
            build()
        except Exception as exc:  # a bad file is reported, not fatal to the others
            errors.append(f"{label}: {exc}")

    hosts_file = src / "hosts.yaml"
    for i, raw in enumerate((_read(hosts_file) or {}).get("hosts", []) if hosts_file.exists() else []):
        def host(raw=raw):
            record = Host.model_validate(raw)
            store.check_id(record.id)
            found["host"][record.id] = store.encode(record)
        attempt(f"hosts.yaml entry {i + 1}", host)

    def models(sub: str, kind: str, model: type[BaseModel]) -> None:
        for path in sorted((src / sub).glob("*.yaml")) if (src / sub).is_dir() else []:
            def one(path=path):
                record = model.model_validate(_read(path))
                store.check_id(record.id)
                found[kind][record.id] = store.encode(record)
            attempt(f"{sub}/{path.name}", one)

    models("agents", "agent", Agent)
    models("decommissioned", "decommissioned", Agent)
    models("cron_jobs", "cron", CronJob)

    for path in sorted((src / "templates").glob("*.yaml")) if (src / "templates").is_dir() else []:
        def template(path=path):
            import json
            found["template"][path.stem] = json.dumps(templates.validate(path.stem, _read(path)))
        attempt(f"templates/{path.name}", template)

    policy_file = src / "policy.yaml"
    if policy_file.exists():
        def pol():
            text = policy_file.read_text()
            policy.parse(text)
            found["policy"][policy.RECORD] = text
        attempt("policy.yaml", pol)

    # An agent whose host is nowhere would fail to deploy and to show up.
    known = set(found["host"]) | set(store.records("host"))
    for agent_id, body in found["agent"].items():
        host_id = Agent.model_validate_json(body).host
        if host_id not in known:
            errors.append(f"agents/{agent_id}.yaml: host {host_id!r} is not in the fleet")
    return found, errors


def import_dir(src: Path, strict: bool = True, actor: str = "import") -> dict:
    """Merge `src` into the store. Returns {"imported": {kind: n}, "errors": [...]}.
    With `strict`, any error means nothing is written."""
    found, errors = collect(src)
    if errors and strict:
        return {"imported": {}, "errors": errors}
    bad = {e.split(":")[0] for e in errors}
    counts = {}
    with opstore.connect() as db:
        for kind, items in found.items():
            counts[kind] = 0
            for id_, body in items.items():
                if kind == "agent" and f"agents/{id_}.yaml" in bad:
                    continue
                counts[kind] += store.put(db, kind, id_, body, action="import", actor=actor)
    return {"imported": counts, "errors": errors}


def export_dir(dst: Path) -> dict[str, int]:
    """Write the store as a fleet/ directory. Existing files with the same
    names are replaced; nothing else in `dst` is touched."""
    dst.mkdir(parents=True, exist_ok=True)
    counts = {}

    def dump(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    hosts = store.list_hosts()
    if hosts:
        dump(dst / "hosts.yaml", {"hosts": [h.model_dump() for h in hosts]})
    counts["hosts"] = len(hosts)
    for kind, sub, listing in (("agents", "agents", store.list_agents()),
                               ("decommissioned", "decommissioned", store.list_decommissioned()),
                               ("cron_jobs", "cron_jobs", cron_store.list_cron_jobs())):
        for record in listing:
            dump(dst / sub / f"{record.id}.yaml", record.model_dump())
        counts[kind] = len(listing)
    names = templates.list_templates()
    for name in names:
        dump(dst / "templates" / f"{name}.yaml", templates.load_template(name))
    counts["templates"] = len(names)
    text = store.find_record(policy.KIND, policy.RECORD)
    if text is not None:
        (dst / "policy.yaml").write_text(text)
    counts["policy"] = int(text is not None)
    return counts


def bootstrap() -> None:
    """On the first start of a store, import the YAML fleet next to the code
    if there is one. Never again: the store is the fleet from then on."""
    with opstore.connect() as db:
        if db.execute("SELECT 1 FROM fleet_meta WHERE key = ?", (BOOTSTRAPPED,)).fetchone():
            return
    src = store.FLEET_DIR
    if src.is_dir() and any(src.rglob("*.yaml")):
        result = import_dir(src, strict=False, actor="bootstrap")
        logger.warning("fleet: imported %s from %s into the store; from now on the store is the fleet "
                       "(export with `python -m backend.fleet export`)", result["imported"], src)
        for error in result["errors"]:
            logger.warning("fleet: not imported: %s", error)
    with opstore.connect() as db:
        db.execute("INSERT OR REPLACE INTO fleet_meta (key, value) VALUES (?, '1')", (BOOTSTRAPPED,))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.fleet", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="merge a fleet/ directory into the store")
    imp.add_argument("dir", nargs="?", type=Path, default=store.FLEET_DIR)
    imp.add_argument("--skip-invalid", action="store_true", help="import the valid records and report the rest")
    exp = sub.add_parser("export", help="write the store out as a fleet/ directory")
    exp.add_argument("dir", nargs="?", type=Path, default=store.FLEET_DIR)
    args = parser.parse_args(argv)
    if args.cmd == "export":
        print("exported:", ", ".join(f"{n} {k}" for k, n in export_dir(args.dir).items()), f"-> {args.dir}")
        return 0
    result = import_dir(args.dir, strict=not args.skip_invalid, actor="cli")
    for error in result["errors"]:
        print(f"error: {error}", file=sys.stderr)
    if result["errors"] and not args.skip_invalid:
        print("nothing imported (use --skip-invalid to import the valid records)", file=sys.stderr)
        return 1
    print("imported:", ", ".join(f"{n} {k}" for k, n in result["imported"].items()))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
