"""sessync -- mirror coding-agent sessions between CLIs.

    sessync push <source_agent> <source_id> <target_agent|all>

    sessync auto_push add|remove <agent>      install/remove that agent's hook
    sessync auto_push list                    which agents push automatically

    sessync auto_pull add|remove <agent>      allow/deny incoming sessions
    sessync auto_pull list                    which agents accept them

    sessync cleanup [--all]                   delete sessions created by sessync

`agent` may be `all`. Pushing is idempotent: an existing mirror is updated in
place rather than duplicated.
"""
from __future__ import annotations

import argparse
import sys

from . import hooks, runtime, sync
from .adapters import ADAPTERS, resolve
from .config import Config

_MARK = {"created": "+", "updated": "~", "unchanged": "=",
         "skipped": "!", "failed": "x"}


def _emit(r: sync.PortResult, cwd: str) -> None:
    print(f"  {_MARK.get(r.action, '?')} {r.tool:12} {r.action:9} {r.session_id or ''}")
    if r.detail:
        print(f"  {'':14} {r.detail}")
    if r.ok and r.session_id:
        print(f"  {'':14} {ADAPTERS[r.tool].resume_hint(r.session_id, cwd)}")


def cmd_push(args) -> int:
    quiet = args.quiet
    try:
        src, src_mod = resolve(args.source_agent)
        if args.target_agent == "all":
            # "all" means every agent that has auto_pull on. Naming one
            # explicitly is an instruction, so it bypasses that setting.
            targets = Config.load().targets_for(src)
            if not targets:
                runtime.log(f"{src}: no agents have auto_pull enabled")
                if not quiet:
                    print("no agents have auto_pull enabled; nothing to do")
                return 0
        else:
            targets = [resolve(args.target_agent)[0]]

        # A hook command is a fixed string and cannot carry an id, so
        # --from-hook takes it from the agent's own payload. Nothing is ever
        # inferred: no id, no push.
        sid = args.source_id
        if args.from_hook and not sid:
            sid = runtime.session_id_from_stdin()
        if not sid:
            runtime.log(f"{src}: no session id given"
                        + (" and none in the hook payload" if args.from_hook else ""))
            if not quiet:
                print("error: source_id is required "
                      "(or --from-hook to read it from the hook payload)",
                      file=sys.stderr)
            return 0 if quiet else 2

        sess = src_mod.read(sid)
        with runtime.Lock():
            results = sync.fanout(src, sid, targets, force=args.force)
        runtime.log(f"{src}/{sid} -> "
                    + "  ".join(f"{r.tool}={r.action}" for r in results))
        if not quiet:
            print(f"{src}/{sid}  ({len(sess.messages)} messages)")
            for r in results:
                _emit(r, sess.cwd)
        return 0 if all(r.ok for r in results) else (0 if quiet else 1)
    except Exception as exc:                           # noqa: BLE001
        # Hooks run this. A non-zero exit degrades the agent it is attached to.
        runtime.log(f"push {args.source_agent}: ERROR {exc!r}")
        if not quiet:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0


def _agents(name: str) -> list[str]:
    return list(ADAPTERS) if name == "all" else [resolve(name)[0]]


def cmd_auto_push(args) -> int:
    if args.action == "list":
        print(f"{'agent':13} {'auto_push':10} hook location")
        for agent, on, where in hooks.status():
            print(f"{agent:13} {str(on).lower():10} {where}")
        return 0
    fn = hooks.install if args.action == "add" else hooks.remove
    for agent in _agents(args.agent):
        try:
            print(fn(agent))
        except Exception as exc:                       # noqa: BLE001
            print(f"{agent}: FAILED {exc}", file=sys.stderr)
    return 0


def cmd_auto_pull(args) -> int:
    cfg = Config.load()
    if args.action == "list":
        print(f"{'agent':13} auto_pull")
        for agent in ADAPTERS:
            print(f"{agent:13} {str(cfg.pulls(agent)).lower()}")
        return 0
    cfg.set(args.agent, args.action == "add")
    cfg.save()
    for agent in _agents(args.agent):
        print(f"{agent}: auto_pull {str(cfg.pulls(agent)).lower()}")
    return 0


def cmd_cleanup(args) -> int:
    results = sync.cleanup(all_sessions=args.all)
    if not results:
        print("no created sessions found to clean up")
        return 0
    print(f"cleaned up {len(results)} session(s):")
    for tool, sid, ok in results:
        status = "deleted" if ok else "not found / already removed"
        print(f"  - {tool:12} {sid} ({status})")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="sessync", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    agents = ", ".join(ADAPTERS)

    pp = sub.add_parser(
        "push", help="mirror one session into another agent, or all of them",
        description="Copy one session from one agent into another, "
                    "or into every agent that has auto_pull on.",
        epilog="\n".join([
            f"agents: {agents}, or 'all' for every auto_pull agent", "",
            "examples:",
            "  sessync push claude ad4ccd05-d797-495f-b5b9-ca6b81c51c0d codex",
            "  sessync push claude ad4ccd05-d797-495f-b5b9-ca6b81c51c0d all"]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    # source_agent and source_id are one address -- keep them adjacent, or the
    # id reads as though it belonged to the target.
    pp.add_argument("source_agent", help="agent to copy the session from")
    pp.add_argument("source_id", nargs="?",
                    help="session id, as the source agent knows it; "
                         "required unless --from-hook is given")
    pp.add_argument("target_agent",
                    help="agent to copy it into, or 'all' for every agent "
                         "with auto_pull on")
    pp.add_argument("--from-hook", action="store_true",
                    help="read the session id from the Stop hook payload on "
                         "stdin, instead of taking it as an argument")
    pp.add_argument("--quiet", action="store_true",
                    help="print nothing and always exit 0, so a failure cannot "
                         "disrupt the agent that ran the hook")
    pp.add_argument("--force", action="store_true",
                    help="overwrite mirrors even if they were continued in "
                         "their own tool since the last push")
    pp.set_defaults(func=cmd_push)

    for name, fn, what, effect in (
            ("auto_push", cmd_auto_push, "push their finished sessions out",
             "adds or removes a Stop hook in that agent's own config"),
            ("auto_pull", cmd_auto_pull, "accept sessions pushed to them",
             "changes ~/.sessync/config.json")):
        sp = sub.add_parser(
            name, help=f"control which agents {what}",
            description=f"Control which agents {what}. {effect[0].upper()}{effect[1:]}.",
            epilog="\n".join([
                f"agents: {agents}, or 'all'", "", "examples:",
                f"  sessync {name} list",
                f"  sessync {name} add codex",
                f"  sessync {name} remove all"]),
            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.add_argument("action", choices=["add", "remove", "list"],
                        help="add: turn on.  remove: turn off.  "
                             "list: show every agent's current setting")
        sp.add_argument("agent", nargs="?", default="all", metavar="agent",
                        help="which agent, or 'all' (default: all). "
                             "ignored by 'list'")
        sp.set_defaults(func=fn)

    cp = sub.add_parser(
        "cleanup", help="delete sessions created by sessync",
        description="Delete session mirrors created by sessync across target agents. "
                    "Leaves original source sessions untouched unless --all is specified.")
    cp.add_argument("--all", action="store_true",
                    help="also delete original source sessions tracked in lineage")
    cp.set_defaults(func=cmd_cleanup)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
