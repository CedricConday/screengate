"""screengate command line."""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from . import __version__
from .applicants import NAMES, cohort
from .gate import extraction_selftest, regrade, run
from .report import render


def _origin_of(name: str) -> str | None:
    for origin, by_gender in NAMES.items():
        for names in by_gender.values():
            if name in names:
                return origin
    return None


def _parse_bias(spec: str | None) -> dict:
    # "tr=1,ar=2,age50=1" -> {"tr": 1, "ar": 2, "age50": 1}
    out = {}
    for part in filter(None, (spec or "").split(",")):
        k, _, v = part.partition("=")
        out[k.strip()] = int(v or 1)
    return out


def _roles(args, stack: ExitStack):
    usage = None
    if args.backend == "offline":
        from .offline import OfflineApplicant, OfflineBot, OfflineExtractor, render_labelled

        bot = OfflineBot(drop=args.drop, fabricate=args.fabricate,
                         bias=_parse_bias(args.bias), origin_of=_origin_of, leak=args.leak)
        return bot, OfflineApplicant(), OfflineExtractor(), render_labelled, None, "offline"
    from .claude import ClaudeApplicant, ClaudeBot, ClaudeExtractor, HttpBot, Usage
    from .offline import render_prose

    usage = Usage()
    client = None
    if args.backend == "subscription":
        from .subscription import CliClient

        client = stack.enter_context(CliClient())
    if args.bot_url:
        bot, label = HttpBot(args.bot_url), f"http {args.bot_url}"
    else:
        bot = ClaudeBot(args.model, args.bot_effort, usage, client)
        label = f"reference bot, {args.model}"
    return (bot, ClaudeApplicant(args.model, "low", usage, client),
            ClaudeExtractor(args.model, "medium", usage, client), render_prose, usage, label)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="screengate",
        description="Plant known facts in synthetic applicants, run a screening bot, "
                    "score what its summary kept, invented, and whether the verdict "
                    "moves when only the name changes.")
    p.add_argument("--version", action="version", version=f"screengate {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("run", "selftest"):
        s = sub.add_parser(name)
        s.add_argument("--backend", choices=("offline", "claude", "subscription"), default="offline")
        s.add_argument("--model", default="claude-opus-5-5")
        s.add_argument("--seed", type=int, default=7)
        s.add_argument("--out", type=Path, default=None)
        s.add_argument("--workers", type=int, default=6)
    r = sub.choices["run"]
    r.add_argument("--n", type=int, default=30, help="applicants")
    r.add_argument("--fairness-n", type=int, default=10, help="interviews re-run under swaps")
    r.add_argument("--swaps", type=int, default=4)
    r.add_argument("--repeats", type=int, default=2)
    r.add_argument("--bot-url", default=None, help="put an HTTP bot through the gate")
    r.add_argument("--bot-effort", default="medium")
    r.add_argument("--no-selftest", action="store_true")
    r.add_argument("--note", default=None, help="one line shown under the report title")
    for s in sub.choices.values():
        # Offline fault knobs; they prove the gate sees each failure.
        s.add_argument("--drop", type=float, default=0.0)
        s.add_argument("--fabricate", type=float, default=0.0)
        s.add_argument("--bias", default=None, help='e.g. "tr=1,age50=1"')
        s.add_argument("--leak", type=float, default=0.0,
                       help="share of chat messages that start with a planning note")
    st = sub.choices["selftest"]
    st.add_argument("--n", type=int, default=40)
    rg = sub.add_parser("regrade", help="grade a saved run again with the current checks; "
                                        "no model calls")
    rg.add_argument("dirs", type=Path, nargs="+", help="run directories holding run.json")

    args = p.parse_args(argv)
    if args.cmd == "regrade":
        return _regrade(args.dirs)
    if getattr(args, "bot_url", None) and args.backend == "offline":
        # The offline backend has its own stand-in bot; a URL there would be
        # ignored and the report would describe the wrong system.
        p.error("--bot-url needs --backend claude or --backend subscription "
                "(the applicant simulator and the extractor still run on Claude)")
    with ExitStack() as stack:
        return _execute(args, stack)


def _execute(args, stack: ExitStack) -> int:
    bot, sim, extractor, renderer, usage, label = _roles(args, stack)
    out = args.out or Path("results") / f"{args.cmd}-{args.backend}-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    log = lambda msg: print(f"[screengate] {msg}", file=sys.stderr, flush=True)

    selftest = None
    if args.cmd == "selftest" or not args.no_selftest:
        selftest = extraction_selftest(extractor, renderer, n=args.n if args.cmd == "selftest"
                                       else 20, seed=args.seed + 4)
        log(f"extractor self-test: {selftest['accuracy']['k']}/{selftest['accuracy']['n']} exact")
        (out / "selftest.json").write_text(json.dumps(selftest, ensure_ascii=False, indent=2))
        if args.cmd == "selftest":
            print(json.dumps(selftest["accuracy"], indent=2))
            return 0

    result = run(bot, sim, extractor, cohort(args.n, args.seed), fairness_n=args.fairness_n,
                 swaps=args.swaps, repeats=args.repeats, seed=args.seed,
                 workers=args.workers, progress=log)
    config = {"bot": label, "seed": args.seed, "n": args.n, "backend": args.backend,
              "model": args.model if args.backend != "offline" else None,
              "faults": {"drop": args.drop, "fabricate": args.fabricate, "bias": args.bias,
                         "leak": args.leak},
              "note": args.note, "version": __version__}
    used = usage.as_dict() if usage else None
    (out / "run.json").write_text(json.dumps({"config": config, "usage": used, **result},
                                             ensure_ascii=False, indent=2))
    (out / "report.md").write_text(render(result, config, selftest, used))
    log(f"wrote {out}/report.md")
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    return 0


def _regrade(dirs: list[Path]) -> int:
    """Rewrite run.json and report.md of saved runs; print what moved.

    Any metric other than ones the current code adds must come out unchanged,
    since the transcripts and extractions are the recorded ones. A change there
    means grading drifted, and the run is left untouched.
    """
    status = 0
    for d in dirs:
        saved = json.loads((d / "run.json").read_text())
        new = regrade(saved)
        old_m, new_m = saved["metrics"], new["metrics"]
        drift = [k for k in old_m if old_m[k] != new_m.get(k)]
        if drift:
            print(f"{d}: metrics changed on regrade: {', '.join(drift)}; not written",
                  file=sys.stderr)
            status = 1
            continue
        st = d / "selftest.json"
        selftest = json.loads(st.read_text()) if st.exists() else None
        (d / "run.json").write_text(json.dumps(new, ensure_ascii=False, indent=2))
        (d / "report.md").write_text(render(new, new["config"], selftest, new.get("usage")))
        added = {k: new_m[k] for k in new_m if k not in old_m}
        print(f"{d}: {json.dumps(added, ensure_ascii=False)}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
