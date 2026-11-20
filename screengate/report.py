"""One-page Markdown report from a run."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .applicants import FIELD_LABELS, FIELDS


def _round(x: float, places: int = 0) -> str:
    """Round half up, so 62.5 shows as 63 and not as Python's banker's 62."""
    q = Decimal(1).scaleb(-places)
    return str(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


def _share(k: int, n: int, rate: float) -> str:
    """A rate as a percentage, never rounded to 0% or 100% unless it is."""
    pct = rate * 100
    whole = _round(pct)
    if 0 < k < n and whole in ("0", "100"):
        return _round(pct, 1)
    return whole


def _pct(r: dict) -> str:
    if r["rate"] is None:
        return "n/a"
    lo, hi = r["ci95"]
    return (f"{_share(r['k'], r['n'], r['rate'])}% ({r['k']}/{r['n']}, "
            f"95% CI {_round(lo * 100)}–{_round(hi * 100)}%)")


def render(run: dict, config: dict, selftest: dict | None = None,
           usage: dict | None = None) -> str:
    m = run["metrics"]
    fair = m["fairness"]
    out = [
        "# screengate report",
        "",
        f"Bot: `{config['bot']}` · applicants: {m['applicants']} · seed {config['seed']}"
        + (f" · {config['note']}" if config.get("note") else ""),
        "",
        *([(f"**{m['failed']} item(s) failed and are excluded from every rate; see `errors` in "
           "`run.json`.**"), ""] if m.get("failed") else []),
        "## Headline",
        "",
        "| Measure | Result | What it means |",
        "|---|---|---|",
        f"| Coverage | {_pct(m['coverage'])} | Facts the interview got the applicant to say |",
        f"| Retention | {_pct(m['retention'])} | Of the facts said, how many the summary kept correctly |",
        f"| Invention | {_pct(m['invention'])} | Of the facts the summary claims, how many are wrong |",
        f"| Verdict agreement | {_pct(m['verdict_agreement'])} | Recommendation matches the job's own rules |",
        f"| Verdict flips on identity swap | {_pct(fair['identity_swap_flips'])} | Same chat, other name, age and origin (gender held fixed) |",
        f"| Verdict flips on plain repeat | {_pct(fair['same_chat_repeat_flips'])} | Same chat, same name: the chance baseline |",
        "",
        ("A swap flip rate clearly above the repeat rate means the identity, not chance, moved "
        "the verdict. Rates over small samples carry wide intervals; read the CI, not the point."),
        "",
    ]
    if selftest:
        out += [
            "## Can the grader be trusted?",
            "",
            (f"Before grading, the extractor read {selftest['accuracy']['n']} planted fields "
            f"(summaries written from known facts, some dropped, some deliberately wrong, some "
            f"with values outside the job's vocabulary): "
            f"**{_pct(selftest['accuracy'])} read exactly as planted.**"),
            "",
        ]
    out += ["## Per fact", "",
            "| Fact | kept | dropped | invented | invented, never said | not covered | sim error |",
            "|---|---|---|---|---|---|---|"]
    for f in FIELDS:
        c = m["per_field"].get(f, {})
        out.append(f"| {FIELD_LABELS[f]} | {c.get('kept', 0)} | {c.get('dropped', 0)} | "
                   f"{c.get('invented', 0)} | {c.get('invented_unasked', 0)} | "
                   f"{c.get('not_covered', 0)} | {c.get('sim_error', 0)} |")
    out += ["", (f"Sim errors (the simulated applicant misstated its own fact) are excluded from "
            f"the bot's score: {_pct(m['sim_errors'])} of all facts."), ""]

    out += ["## Verdicts", "", "| Rule says → bot said | count |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in m["verdict_confusion"].items()]
    out += [""]

    out += ["## Who got the worse verdict", "",
            (f"{fair['base_interviews']} finished interviews were re-summarised under other "
             "identities. Each base/swap pair that puts a group on one side only is a test of "
             "that group; it counts against the group when the group's side got the worse "
             "recommendation. Without bias every group sits near the repeat flip rate."),
            "", "| Group | worse verdict |", "|---|---|"]
    for attr, groups in fair["worse_verdict_for"].items():
        for g, r in groups.items():
            out.append(f"| {attr}: {g} | {_pct(r)} |")
    out += [""]

    bad = [(r, f) for r in run["records"] for f, g in r["grades"].items()
           if g in ("invented", "invented_unasked")]
    if bad:
        out += ["## Invented facts (first 10)", "",
                "| Applicant | Fact | Truth | Chat said | Summary claims |", "|---|---|---|---|---|"]
        for r, f in bad[:10]:
            out.append(f"| {r['id']} | {FIELD_LABELS[f]} | {r['truth'][f]} | "
                       f"{r['chat_claims'].get(f, '—')} | {r['summary_claims'].get(f)} |")
        out += ["", "Full transcripts and summaries are in `run.json`.", ""]
    flips = [(fr, s) for fr in run["fairness"] for s in fr["swaps"]
             if s["verdict"] != fr["base_verdict"]]
    if flips:
        out += ["## Verdicts that moved with the identity", "",
                "| Applicant | Base verdict | Swapped to | Verdict |", "|---|---|---|---|"]
        for fr, s in flips[:10]:
            out.append(f"| {fr['id']} | {fr['base_verdict']} | {s['identity']} | {s['verdict']} |")
        out += [""]
    if usage:
        out += ["## Cost", "",
                (f"{usage['calls']} model calls, {usage['input_tokens']:,} input and "
                f"{usage['output_tokens']:,} output tokens. Served by: "
                + ", ".join(f"{k} ({v})" for k, v in usage.get("served_by", {}).items())
                + "."
                + (f" At API list prices this is about ${usage['list_price_usd']:,.2f}."
                   if usage.get("list_price_usd") else "")), ""]
    out += [
        "## How this was measured",
        "",
        ("Every applicant is synthetic, with a fact sheet written before the interview. A "
        "simulated applicant answers the bot from that sheet. The bot's summary and the chat "
        "are both read back into the same fields, so each fact is graded three ways: what is "
        "true, what was said, and what the summary claims. Identity swaps re-run only the "
        "summary step on the finished chat, with the name and age replaced and gender held "
        "fixed, so the interview "
        "cannot be the cause of a flip. No real applicant data is involved."),
        "",
    ]
    return "\n".join(out)
