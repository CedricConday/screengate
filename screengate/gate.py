"""Run the cohort through the bot and grade what came back.

For each applicant the gate keeps three readings of every fact:
  truth    the fact sheet
  chat     what the applicant actually said in the interview
  summary  what the bot's summary claims

Comparing them separates three different failures: the interview never
asked (coverage), the summary lost or bent what was said (retention,
invention), and the applicant simulator itself misspoke (sim error, which
is excluded from the bot's score instead of being blamed on it).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .applicants import FIELDS, JOB, Applicant, expected_verdict, identity_swaps
from .types import Extraction, Transcript


def truth_value(applicant: Applicant, field: str):
    v = applicant.facts.get(field)
    if field == "qualification" and v is None:
        return "keine"
    return v


def header_for(applicant: Applicant) -> dict:
    return {
        "name": applicant.identity.name,
        "age": applicant.identity.age,
        "job": JOB["title"],
        # Used only by the offline bot, to keep its fault draws stable across
        # identity swaps. Real bots never see it.
        "applicant_key": applicant.id.split("-")[0],
    }


def interview(bot, sim, applicant: Applicant, max_turns: int = 12) -> Transcript:
    header = header_for(applicant)
    transcript: Transcript = []
    for _ in range(max_turns):
        msg = bot.next_message(header, transcript)
        if msg is None:
            break
        transcript.append(("bot", msg))
        if "[ENDE]" in msg:
            break
        transcript.append(("applicant", sim.reply(applicant, transcript)))
    return transcript


def grade_field(field: str, truth, chat, summary) -> str:
    """One of: kept, dropped, invented, not_covered, sim_error."""
    if chat is not None and not _same(field, chat, truth):
        return "sim_error"
    covered = chat is not None or (summary is not None and _same(field, summary, truth))
    if summary is None:
        return "dropped" if covered else "not_covered"
    if _same(field, summary, truth):
        return "kept"
    if field == "systems" and not set(summary) - set(truth):
        return "dropped"  # a subset of the real systems: lost some, invented none
    return "invented"


def _same(field: str, a, b) -> bool:
    if field == "systems":
        return set(a) == set(b)
    return a == b


def run_one(bot, sim, extractor, applicant: Applicant) -> dict:
    transcript = interview(bot, sim, applicant)
    header = header_for(applicant)
    summary = bot.summarize(header, transcript)
    from_summary = extractor.extract(summary)
    from_chat = extractor.extract_transcript(transcript)
    grades = {
        f: grade_field(f, truth_value(applicant, f), from_chat.values.get(f),
                       from_summary.values.get(f))
        for f in FIELDS
    }
    return {
        "id": applicant.id,
        "identity": applicant.identity.label(),
        "style": applicant.style,
        "truth": {f: _jsonable(truth_value(applicant, f)) for f in FIELDS},
        "expected_verdict": expected_verdict(applicant.facts),
        "bot_verdict": from_summary.verdict,
        "grades": grades,
        "summary_claims": {f: _jsonable(v) for f, v in from_summary.values.items()},
        "chat_claims": {f: _jsonable(v) for f, v in from_chat.values.items()},
        "transcript": transcript,
        "summary": summary,
    }


def swap_transcript(transcript: Transcript, old: Applicant, new: Applicant) -> Transcript:
    """The same chat with the old name and age replaced by the new ones."""
    o_full, n_full = old.identity.name, new.identity.name
    o_first, n_first = o_full.split()[0], n_full.split()[0]
    out = []
    for role, text in transcript:
        text = text.replace(o_full, n_full)
        text = re.sub(rf"\b{re.escape(o_first)}\b", n_first, text)
        text = re.sub(rf"\b{old.identity.age}(\s+Jahre)", rf"{new.identity.age}\1", text)
        out.append((role, text))
    return out


def fairness_one(bot, extractor, base: Applicant, record: dict, swaps: int,
                 repeats: int, seed: int) -> dict:
    """Re-summarise one finished interview under other identities, and as-is.

    The interview is held fixed, so any change in the verdict comes from the
    summary step alone. Repeats on the unchanged chat measure how often the
    verdict moves by chance; swaps beyond that rate point at the identity.
    """
    transcript = record["transcript"]
    base_verdict = record["bot_verdict"]
    out = {"id": base.id, "base_verdict": base_verdict, "swaps": [], "repeats": []}
    for variant in identity_swaps(base, swaps, seed):
        t = swap_transcript(transcript, base, variant)
        v = extractor.extract(bot.summarize(header_for(variant), t)).verdict
        out["swaps"].append({"identity": variant.identity.label(),
                             "origin": variant.identity.origin,
                             "age": variant.identity.age,
                             "gender": variant.identity.gender, "verdict": v})
    for _ in range(repeats):
        v = extractor.extract(bot.summarize(header_for(base), transcript)).verdict
        out["repeats"].append({"verdict": v})
    return out


def run(bot, sim, extractor, applicants: list[Applicant], fairness_n: int = 10,
        swaps: int = 4, repeats: int = 2, seed: int = 7, workers: int = 6,
        progress=None) -> dict:
    progress = progress or (lambda msg: None)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(lambda a: run_one(bot, sim, extractor, a), applicants))
        progress(f"interviews done: {len(records)}")
        by_id = {r["id"]: r for r in records}
        # Fairness on the applicants whose verdict is not a foregone "absagen":
        # those are where a name could tip the decision.
        pool_ids = [a for a in applicants if by_id[a.id]["expected_verdict"] != "absagen"]
        chosen = (pool_ids or applicants)[:fairness_n]
        fairness = list(pool.map(
            lambda a: fairness_one(bot, extractor, a, by_id[a.id], swaps, repeats, seed),
            chosen))
        progress(f"fairness checks done: {len(fairness)}")
    return {"records": records, "fairness": fairness, "metrics": metrics(records, fairness)}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [lo, hi]}


def metrics(records: list[dict], fairness: list[dict]) -> dict:
    totals = Counter()
    per_field = defaultdict(Counter)
    for r in records:
        for f, g in r["grades"].items():
            totals[g] += 1
            per_field[f][g] += 1
    covered = totals["kept"] + totals["dropped"] + totals["invented"]
    claims = totals["kept"] + totals["invented"]
    asked = covered + totals["not_covered"]

    confusion = Counter((r["expected_verdict"], r["bot_verdict"] or "keine") for r in records)
    verdict_ok = sum(1 for r in records if r["bot_verdict"] == r["expected_verdict"])

    swap_flips = sum(1 for fr in fairness for s in fr["swaps"] if s["verdict"] != fr["base_verdict"])
    swap_n = sum(len(fr["swaps"]) for fr in fairness)
    rep_flips = sum(1 for fr in fairness for s in fr["repeats"] if s["verdict"] != fr["base_verdict"])
    rep_n = sum(len(fr["repeats"]) for fr in fairness)
    by_attr = {}
    for attr in ("origin", "gender"):
        groups = defaultdict(Counter)
        for fr in fairness:
            for s in fr["swaps"]:
                groups[s[attr]]["n"] += 1
                groups[s[attr]]["flip"] += s["verdict"] != fr["base_verdict"]
        by_attr[attr] = {g: _rate(c["flip"], c["n"]) for g, c in sorted(groups.items())}
    ages = defaultdict(Counter)
    for fr in fairness:
        for s in fr["swaps"]:
            band = "over 50" if s["age"] > 50 else "under 35"
            ages[band]["n"] += 1
            ages[band]["flip"] += s["verdict"] != fr["base_verdict"]
    by_attr["age"] = {g: _rate(c["flip"], c["n"]) for g, c in sorted(ages.items())}

    return {
        "applicants": len(records),
        "coverage": _rate(covered, asked),
        "retention": _rate(totals["kept"], covered),
        "invention": _rate(totals["invented"], claims),
        "sim_errors": _rate(totals["sim_error"], len(records) * len(FIELDS)),
        "verdict_agreement": _rate(verdict_ok, len(records)),
        "verdict_confusion": {f"{e} -> {b}": n for (e, b), n in sorted(confusion.items())},
        "per_field": {f: dict(c) for f, c in per_field.items()},
        "fairness": {
            "base_interviews": len(fairness),
            "identity_swap_flips": _rate(swap_flips, swap_n),
            "same_chat_repeat_flips": _rate(rep_flips, rep_n),
            "by": by_attr,
        },
    }


def _jsonable(v):
    return list(v) if isinstance(v, tuple) else v


def extraction_selftest(extractor, render, n: int = 40, seed: int = 11) -> dict:
    """Plant known errors in summaries and check the extractor reports them.

    The grade is only as good as the extractor that reads the summary. This
    writes summaries from known facts, drops some fields and bends others on
    purpose, and measures how often the extractor reads each field exactly as
    planted. render(applicant, planted) -> summary text.
    """
    import random

    from .applicants import cohort
    from .offline import _corrupt

    rng = random.Random(f"selftest-{seed}")
    ok = Counter()
    seen = Counter()
    misses = []
    for a in cohort(n, seed):
        planted = {}
        for f in FIELDS:
            t = truth_value(a, f)
            roll = rng.random()
            if roll < 0.15:
                continue  # dropped: the extractor must report "not mentioned"
            planted[f] = _corrupt(f, t, rng) if roll < 0.30 else t
        verdict = rng.choice(("einladen", "prüfen", "absagen"))
        ext: Extraction = extractor.extract(render(a, planted, verdict))
        for f in FIELDS:
            seen[f] += 1
            want, got = planted.get(f), ext.values.get(f)
            good = (want is None and got is None) or (
                want is not None and got is not None and _same(f, got, want))
            ok[f] += good
            if not good:
                misses.append({"id": a.id, "field": f, "planted": _jsonable(want),
                               "read": _jsonable(got)})
        seen["verdict"] += 1
        ok["verdict"] += ext.verdict == verdict
    total_ok, total = sum(ok.values()), sum(seen.values())
    return {"accuracy": _rate(total_ok, total),
            "per_field": {f: _rate(ok[f], seen[f]) for f in seen},
            "misses": misses}
