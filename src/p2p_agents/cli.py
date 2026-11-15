"""Command line: `p2p <command> --help`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from p2p_agents import store
from p2p_agents.models import DiffReport, ReplicationPlan, Snapshot


def _types(value: str | None) -> list[str] | None:
    return [t.strip() for t in value.split(",") if t.strip()] if value else None


def _snapshots(agent, args) -> tuple[Snapshot, Snapshot]:
    if args.from_snapshots:
        pairs = []
        for inst in (args.source, args.target):
            snap = store.latest(agent.platform.name, inst)
            if snap is None:
                sys.exit(f"no saved snapshot for {inst}; run `p2p snapshot` first or drop --from-snapshots")
            pairs.append(snap)
        return pairs[0], pairs[1]
    types = _types(args.types)
    return agent.snapshot(args.source, types), agent.snapshot(args.target, types)


def print_report(report: DiffReport) -> None:
    c = report.counts()
    print(f"\n{report.platform}: {report.source_instance} -> {report.target_instance}")
    print(f"  added {c['added']}  changed {c['changed']}  removed {c['removed']}  unchanged {c['unchanged']}")
    for d in report.diffs:
        if d.status == "unchanged":
            continue
        print(f"  [{d.status:7}] {d.object_type}:{d.key}")
        for ch in d.changes[:8]:
            print(f"             {ch.path}: {ch.target!r} -> {ch.source!r}")
        if len(d.changes) > 8:
            print(f"             ... {len(d.changes) - 8} more")
    if report.issues:
        print("\nIssues:")
        for i in report.issues:
            print(f"  {i.severity.upper():7} {i.code} {i.object_type}:{i.key} - {i.message}")


def print_plan(plan: ReplicationPlan) -> None:
    print(f"\nPlan {plan.source_instance} -> {plan.target_instance}: {len(plan.actions)} action(s)")
    for n, a in enumerate(plan.actions, 1):
        print(f"  {n:3}. {a.action:6} {a.item.object_type}:{a.item.key}")
    if plan.skipped_removals:
        print(f"  not removed (do it deliberately): {', '.join(plan.skipped_removals)}")
    for i in plan.issues:
        print(f"  {i.severity.upper():7} {i.code} {i.object_type}:{i.key} - {i.message}")
    print("  status: " + ("BLOCKED" if plan.blocked else "ready"))


def cmd_snapshot(args) -> None:
    from p2p_agents.agents.config_replication import ConfigReplicationAgent

    agent = ConfigReplicationAgent(args.platform)
    snap = agent.snapshot(args.instance, _types(args.types), save=False)
    path = store.save(snap)
    print(f"{len(snap.items)} objects from {args.instance} -> {path}")


def cmd_diff(args) -> None:
    from p2p_agents.agents.config_replication import ConfigReplicationAgent

    agent = ConfigReplicationAgent(args.platform)
    source, target = _snapshots(agent, args)
    report = agent.diff(source, target)
    print_report(report)
    if args.json:
        Path(args.json).write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nreport written to {args.json}")
    if args.explain:
        print("\n" + agent.explain(report))
    sys.exit(1 if report.has_blockers else 0)


def cmd_replicate(args) -> None:
    from p2p_agents.agents.config_replication import ConfigReplicationAgent
    from p2p_agents.replication import PlanBlocked

    agent = ConfigReplicationAgent(args.platform)
    source, target = _snapshots(agent, args)
    report = agent.diff(source, target)
    plan = agent.plan(report, source, target, set(args.select) if args.select else None)
    print_plan(plan)
    if not args.apply:
        print("\nDry run. Re-run with --apply to execute.")
        sys.exit(1 if plan.blocked else 0)
    try:
        results = agent.apply(plan, Path(args.audit_dir), approved_by=args.approved_by)
    except PlanBlocked as e:
        sys.exit(f"not applied: {e}")
    for r in results:
        print(f"  {'OK  ' if r.ok else 'FAIL'} {r.action} {r.object_type}:{r.key}" + (f" - {r.error}" if r.error else ""))
    sys.exit(0 if all(r.ok for r in results) and len(results) == len(plan.actions) else 1)


def cmd_docgen(args) -> None:
    from p2p_agents.agents.docugen import DocuGenAgent, is_stale
    from p2p_agents.replication import compare
    from p2p_agents.settings import load_platform

    platform = load_platform(args.platform)
    snap = store.latest(args.platform, args.instance)
    if snap is None:
        sys.exit(f"no saved snapshot for {args.instance}; run `p2p snapshot` first")
    if args.types:
        wanted = set(_types(args.types) or [])
        snap = snap.model_copy(update={"items": [i for i in snap.items if i.object_type in wanted],
                                       "object_types": [t for t in snap.object_types if t in wanted]})
    out = Path(args.out) / f"{args.platform}-{args.instance}-{args.kind}.md"
    if args.check:
        stale = is_stale(out, snap)
        print(f"{out}: {'STALE' if stale else 'up to date'}")
        sys.exit(1 if stale else 0)
    if not args.force and not is_stale(out, snap):
        print(f"{out} already matches snapshot {snap.fingerprint()}; use --force to regenerate")
        return
    diff = None
    if args.compare_to:
        other = store.latest(args.platform, args.compare_to)
        if other is None:
            sys.exit(f"no saved snapshot for {args.compare_to}")
        diff = compare(snap, other, platform)
    notes = Path(args.notes).read_text(encoding="utf-8") if args.notes else ""
    existing = out.read_text(encoding="utf-8") if out.exists() else ""
    doc = DocuGenAgent().generate(args.kind, snap, platform.display_name, diff, notes, existing)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out}")


def _triage_one(agent, number: str, apply: bool) -> None:
    from p2p_agents.agents.triage import work_note

    incident, result = agent.triage(number)
    group = agent.routing.group_for(result)
    print(f"\n=== {number}: {incident.get('short_description')}")
    print(work_note(result, group))
    if apply:
        agent.dispatch(incident, result)
        print(f"\n-> work note posted, routed to {group}")
    else:
        print("\n(dry run: nothing written to ServiceNow; use --apply)")


def cmd_triage(args) -> None:
    from p2p_agents.agents.triage import Routing, TriageAgent
    from p2p_agents.servicenow import ServiceNowClient

    agent = TriageAgent(ServiceNowClient.from_env(), Routing.load(Path(args.routing)))
    numbers = args.numbers
    if args.queue:
        numbers = [i["number"] for i in agent.sn.new_incidents(args.queue, args.limit)]
        print(f"{len(numbers)} new incident(s) in {args.queue}")
    for number in numbers:
        try:
            _triage_one(agent, number, args.apply)
        except Exception as e:  # noqa: BLE001 - keep going through the queue
            print(f"\n=== {number}: triage failed: {e}", file=sys.stderr)


STAGE_LABEL = {"passed": "PASSED", "failed": "FAILED", "blocked": "BLOCKED", "pending": "PENDING", "planned": "PLANNED"}


def _rollout_agent(args):
    from p2p_agents.agents.template_rollout import TemplateRolloutAgent

    answers = Path(args.answers)
    run_dir = Path(args.run_dir) if args.run_dir else Path("rollout") / f"{Path(args.template).stem}-{answers.stem}"
    return TemplateRolloutAgent(args.template, answers, run_dir)


def print_stage(stage) -> None:
    from p2p_agents.rollout.verify import summarize

    c = summarize(stage.tests)
    creates = sum(a["action"] == "create" for a in stage.actions)
    updates = sum(a["action"] == "update" for a in stage.actions)
    print(f"\n=== {stage.instance}: {STAGE_LABEL[stage.status]}" + (f" — {stage.note}" if stage.note else ""))
    print(f"  changes: {creates} create, {updates} update"
          + (" (applied)" if stage.applied else "")
          + f" | tests: {c['pass']} pass, {c['fail']} fail, {c['pending']} pending, {c['skip']} skipped")
    for a in [] if stage.applied else stage.actions[:40]:
        print(f"    {a['action']:6} {a['object_type']}:{a['key']}")
    for r in stage.results:
        if not r.ok:
            print(f"  FAIL {r.action} {r.object_type}:{r.key} - {r.error}")
    for i in stage.issues:
        print(f"  {i.severity.upper()} {i.code} {i.object_type}:{i.key} - {i.message}")
    for t in stage.tests:
        if t.status in ("fail", "pending"):
            print(f"  {t.status.upper():7} {t.id} [{t.entities}] {t.description} - {t.detail}")
    for f in stage.import_files:
        print(f"  import file: {f}")


def cmd_rollout_templates(args) -> None:
    from p2p_agents.rollout.template import list_templates, load_template

    for name, path in list_templates().items():
        t = load_template(str(path))
        print(f"{name:28} {t.platform:8} {len(t.params)} questions, {len(t.objects)} objects, "
              f"{len(t.checks)} checks, {len(t.uat)} UAT cases — {t.description}")


def cmd_rollout_questions(args) -> None:
    from p2p_agents.rollout.template import answers_template, ask, load_answers, load_template, write_answers

    template = load_template(args.template)
    out = Path(args.out)
    if args.interactive:
        existing = load_answers(out) if out.exists() else []
        entities = ask(template, existing)
        write_answers(entities, template, out)
        print(f"\n{len(entities)} entit{'y' if len(entities) == 1 else 'ies'} saved to {out}")
        return
    print(f"Questions for {template.name} ({template.description}):")
    for p in template.params.values():
        extra = [x for x in (f"e.g. {p.example}" if p.example else "",
                             "optional" if not p.required else "",
                             f"default {p.default}" if p.default is not None else "") if x]
        print(f"  {p.name:24} {p.question}" + (f"  ({'; '.join(extra)})" if extra else ""))
    answers_template(template, out)
    print(f"\nAnswer file with an example row written to {out}; add one row per company code.")


def _ai_error(e: Exception) -> str:
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return "the Anthropic API key was rejected (check ANTHROPIC_API_KEY)"
    if isinstance(e, anthropic.APIError):
        return f"Claude API error: {getattr(e, 'message', e)}"
    return str(e)


def cmd_rollout_run(args) -> None:
    agent = _rollout_agent(args)
    stages = [s.strip() for arg in args.stages for s in arg.split(",") if s.strip()]
    print(f"Template {agent.template.name}: {len(agent.rendered.entities)} entities "
          f"({', '.join(agent.rendered.entities)}), {len(agent.rendered.items)} configuration objects")
    previous = None
    for name in stages:
        stage = agent.run_stage(name, apply=args.apply, approved_by=args.approved_by, previous=previous)
        print_stage(stage)
        if args.apply and stage.status != "passed":
            later = stages[stages.index(name) + 1:]
            if later:
                print(f"\nStopped: {', '.join(later)} not changed because {name} did not pass.")
            break
        previous = name
    if args.review and args.apply:
        last = [s for s in stages if s in agent.state.stages][-1]
        try:
            print(f"\nQA review: {agent.review(last)}")
        except Exception as e:  # noqa: BLE001 - the roll-out itself is done; report and carry on
            print(f"\nQA review skipped: {_ai_error(e)}")
    try:
        csv_path, md_path, n = agent.uat(ai=args.ai_uat)
    except Exception as e:  # noqa: BLE001
        if not args.ai_uat:
            raise
        print(f"\nAI UAT cases skipped: {_ai_error(e)}")
        csv_path, md_path, n = agent.uat(ai=False)
    print(f"\nUAT scripts ({n} cases): {csv_path}")
    print(f"Checklist: {agent.run_dir / 'checklist.md'} (and checklist.csv)")
    if not args.apply:
        print("\nDry run. Re-run with --apply to configure.")
    wanted = ("passed",) if args.apply else ("planned", "passed")
    ok = all(s in agent.state.stages and agent.state.stages[s].status in wanted for s in stages)
    sys.exit(0 if ok else 1)


def cmd_rollout_verify(args) -> None:
    agent = _rollout_agent(args)
    stage = agent.verify_stage(args.instance)
    print_stage(stage)
    if args.review:
        print(f"\nQA review: {agent.review(args.instance)}")
    print(f"\nChecklist: {agent.run_dir / 'checklist.md'}")
    sys.exit(0 if stage.status == "passed" else 1)


def cmd_rollout_uat(args) -> None:
    agent = _rollout_agent(args)
    csv_path, md_path, n = agent.uat(ai=args.ai)
    print(f"{n} UAT cases written to {csv_path} and {md_path}")


def _terminal_approval(summary: str) -> bool:
    print(f"\n  ⚠  The copilot wants to change systems:\n     {summary}")
    try:
        return input("     Approve? type 'yes' to run: ").strip().lower() in ("yes", "y", "haan", "ha")
    except EOFError:
        return False


def cmd_ask(args) -> None:
    from p2p_agents.agents.copilot import CopilotAgent

    copilot = CopilotAgent(approve=_terminal_approval)
    print(copilot.ask(" ".join(args.question)))


def cmd_chat(args) -> None:
    from p2p_agents.agents.copilot import CopilotAgent

    copilot = CopilotAgent(approve=_terminal_approval)
    print("P2P Copilot — ask in English or Hindi. 'new' starts over, 'exit' quits.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text.lower() in ("exit", "quit", "q"):
            return
        if text.lower() == "new":
            copilot.reset()
            continue
        if not text:
            continue
        try:
            print("\ncopilot> " + copilot.ask(text))
        except Exception as e:  # noqa: BLE001 - keep the chat open
            print(f"\ncopilot> (error) {_ai_error(e)}")


def cmd_cockpit(args) -> None:
    from p2p_agents.cockpit import serve

    if args.open:
        import threading
        import webbrowser

        threading.Timer(0.8, webbrowser.open, [f"http://{args.host}:{args.port}"]).start()
    serve(args.host, args.port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="p2p", description="Agentic P2P orchestration and run-support")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot", help="extract configuration from one instance")
    p.add_argument("--platform", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--types", help="comma-separated object types (default: all)")
    p.set_defaults(func=cmd_snapshot)

    for name, func, help_ in (("diff", cmd_diff, "compare two instances"),
                              ("replicate", cmd_replicate, "plan (and optionally apply) a replication")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--platform", required=True)
        p.add_argument("--source", required=True)
        p.add_argument("--target", required=True)
        p.add_argument("--types")
        p.add_argument("--from-snapshots", action="store_true", help="use the latest saved snapshots")
        p.set_defaults(func=func)
        if name == "diff":
            p.add_argument("--explain", action="store_true", help="add an AI release-risk summary")
            p.add_argument("--json", help="write the diff report to this file")
        else:
            p.add_argument("--select", nargs="*", help="object types or type:key to include")
            p.add_argument("--apply", action="store_true")
            p.add_argument("--approved-by", help="required when the target is production")
            p.add_argument("--audit-dir", default="audit")

    p = sub.add_parser("docgen", help="generate or check FRD/TDD documents")
    p.add_argument("--platform", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--kind", choices=["frd", "tdd"], required=True)
    p.add_argument("--types")
    p.add_argument("--compare-to", help="instance to diff against for a release change log")
    p.add_argument("--notes", help="file with project notes to include")
    p.add_argument("--out", default="docs/generated")
    p.add_argument("--check", action="store_true", help="only report whether the document is stale")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_docgen)

    p = sub.add_parser("triage", help="triage ServiceNow incidents")
    p.add_argument("numbers", nargs="*", help="incident numbers, e.g. INC0012345")
    p.add_argument("--queue", help="triage new incidents in this assignment group instead")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--routing", default="config/routing.toml")
    p.add_argument("--apply", action="store_true", help="post work notes and route in ServiceNow")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("rollout", help="standard configuration from a template for many company codes")
    rsub = p.add_subparsers(dest="rollout_command", required=True)
    r = rsub.add_parser("templates", help="list available templates")
    r.set_defaults(func=cmd_rollout_templates)
    r = rsub.add_parser("questions", help="show the questions and write an answers file to fill in")
    r.add_argument("--template", required=True)
    r.add_argument("--out", required=True, help="answers file to write (.csv for bulk, or .toml)")
    r.add_argument("--interactive", action="store_true", help="ask the questions here and save the answers")
    r.set_defaults(func=cmd_rollout_questions)

    def rollout_common(r):
        r.add_argument("--template", required=True)
        r.add_argument("--answers", required=True, help="answers file: CSV with one row per company code, or TOML")
        r.add_argument("--run-dir", help="folder for state, checklist, tests and UAT (default rollout/<template>-<answers>)")

    r = rsub.add_parser("run", help="plan, configure and test each environment in order (e.g. DEV then QA)")
    rollout_common(r)
    r.add_argument("--stages", required=True, nargs="+", help="instances in promotion order, e.g. coupa-dev coupa-qa (commas work too)")
    r.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    r.add_argument("--approved-by", help="required when a stage is production")
    r.add_argument("--review", action="store_true", help="AI QA review of the last environment")
    r.add_argument("--ai-uat", action="store_true", help="add AI-suggested UAT cases to the template's cases")
    r.set_defaults(func=cmd_rollout_run)
    r = rsub.add_parser("verify", help="re-test one environment without changing it")
    rollout_common(r)
    r.add_argument("--instance", required=True)
    r.add_argument("--review", action="store_true")
    r.set_defaults(func=cmd_rollout_verify)
    r = rsub.add_parser("uat", help="write UAT test scripts")
    rollout_common(r)
    r.add_argument("--ai", action="store_true", help="add AI-suggested cases")
    r.set_defaults(func=cmd_rollout_uat)

    p = sub.add_parser("ask", help="ask the P2P Copilot one question (English or Hindi)")
    p.add_argument("question", nargs="+")
    p.set_defaults(func=cmd_ask)
    p = sub.add_parser("chat", help="conversation with the P2P Copilot")
    p.set_defaults(func=cmd_chat)
    p = sub.add_parser("cockpit", help="Go-Live Cockpit web page with the Copilot built in")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--open", action="store_true", help="open it in the browser")
    p.set_defaults(func=cmd_cockpit)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.command == "triage" and not (args.numbers or args.queue):
        parser.error("give incident numbers or --queue")
    try:
        args.func(args)
    except (RuntimeError, ValueError, LookupError, OSError) as e:
        if args.verbose:
            raise
        sys.exit(f"error: {e}")
    except Exception as e:
        import anthropic

        if args.verbose or not isinstance(e, anthropic.APIError):
            raise
        sys.exit(f"error: {_ai_error(e)}")


if __name__ == "__main__":
    main()
