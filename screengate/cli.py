"""screengate command line."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .applicants import NAMES, cohort
from .gate import extraction_selftest, run
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


def _roles(args):
    usage = None
    if args.backend == "offline":
        from .offline import OfflineApplicant, OfflineBot, OfflineExtractor, render_labelled

        bot = OfflineBot(drop=args.drop, fabricate=args.fabricate,
                         bias=_parse_bias(args.bias), origin_of=_origin_of)
        return bot, OfflineApplicant(), OfflineExtractor(), render_labelled, None, "offline"
    from .claude import ClaudeApplicant, ClaudeBot, ClaudeExtractor, HttpBot, Usage
    from .offline import render_prose

    usage = Usage()
    if args.bot_url:
        bot, label = HttpBot(args.bot_url), f"http {args.bot_url}"
    else:
        bot, label = ClaudeBot(args.model, args.bot_effort, usage), f"reference bot, {args.model}"
    return (bot, ClaudeApplicant(args.model, "low", usage),
            ClaudeExtractor(args.model, "medium", usage), render_prose, usage, label)


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
        s.add_argument("--backend", choices=("offline", "claude"), default="offline")
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
    st = sub.choices["selftest"]
    st.add_argument("--n", type=int, default=40)

    args = p.parse_args(argv)
    bot, sim, extractor, renderer, usage, label = _roles(args)
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
              "model": args.model if args.backend == "claude" else None,
              "faults": {"drop": args.drop, "fabricate": args.fabricate, "bias": args.bias},
              "note": args.note, "version": __version__}
    used = usage.as_dict() if usage else None
    (out / "run.json").write_text(json.dumps({"config": config, "usage": used, **result},
                                             ensure_ascii=False, indent=2))
    (out / "report.md").write_text(render(result, config, selftest, used))
    log(f"wrote {out}/report.md")
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
