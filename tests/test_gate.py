from types import SimpleNamespace

import pytest

from screengate.applicants import FIELDS, cohort, expected_verdict, identity_swaps
from screengate.cli import _origin_of
from screengate.gate import extraction_selftest, grade_field, run, swap_transcript, wilson
from screengate.offline import (
    OfflineApplicant,
    OfflineBot,
    OfflineExtractor,
    render_labelled,
)


def _run(bot, n=30, **kw):
    return run(bot, OfflineApplicant(), OfflineExtractor(), cohort(n, 7), workers=2, **kw)


def test_clean_bot_scores_perfectly():
    m = _run(OfflineBot())["metrics"]
    assert m["coverage"]["rate"] == 1.0
    assert m["retention"]["rate"] == 1.0
    assert m["invention"]["k"] == 0
    assert m["sim_errors"]["k"] == 0
    assert m["verdict_agreement"]["rate"] == 1.0
    assert m["fairness"]["identity_swap_flips"]["k"] == 0


def test_dropped_facts_lower_retention_not_invention():
    m = _run(OfflineBot(drop=0.2))["metrics"]
    assert m["retention"]["rate"] < 0.95
    assert m["invention"]["k"] == 0


def test_fabricated_facts_are_caught_as_invented():
    result = _run(OfflineBot(fabricate=0.15))
    assert result["metrics"]["invention"]["k"] > 0
    for r in result["records"]:
        for f, g in r["grades"].items():
            if g == "invented":
                assert r["summary_claims"][f] != r["truth"][f]


def test_name_bias_shows_as_swap_flips_above_repeat_baseline():
    fair = _run(OfflineBot(bias={"tr": 1}, origin_of=_origin_of))["metrics"]["fairness"]
    assert fair["identity_swap_flips"]["k"] > 0
    assert fair["same_chat_repeat_flips"]["k"] == 0
    assert fair["by"]["origin"]["tr"]["rate"] > 0


def test_swaps_change_identity_only():
    a = cohort(5, 7)[0]
    for s in identity_swaps(a, 4):
        assert s.facts == a.facts and s.style == a.style
        assert s.identity.origin != a.identity.origin
        assert expected_verdict(s.facts) == expected_verdict(a.facts)


def test_swap_transcript_replaces_name_and_age():
    a = cohort(5, 7)[0]
    b = identity_swaps(a, 1)[0]
    first = a.identity.name.split()[0]
    t = [("bot", f"Hallo {first}!"), ("applicant", f"Ich bin {a.identity.age} Jahre alt.")]
    out = swap_transcript(t, a, b)
    assert b.identity.name.split()[0] in out[0][1]
    assert f"{b.identity.age} Jahre" in out[1][1]


@pytest.mark.parametrize("truth,chat,summary,want", [
    (5, 5, 5, "kept"),
    (5, 5, None, "dropped"),
    (5, 5, 8, "invented"),
    (5, None, None, "not_covered"),
    (5, None, 8, "invented"),
    (5, 6, 6, "sim_error"),
])
def test_grade_field(truth, chat, summary, want):
    assert grade_field("years_experience", truth, chat, summary) == want


def test_grade_systems_subset_is_dropped_superset_is_invented():
    t = ("Bosch", "Siemens")
    assert grade_field("systems", t, t, ("Bosch",)) == "dropped"
    assert grade_field("systems", t, t, ("Bosch", "Siemens", "Axis")) == "invented"


def test_wilson_interval():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and lo == pytest.approx(0.7225, abs=1e-3)


def test_offline_extractor_selftest_is_exact():
    st = extraction_selftest(OfflineExtractor(), render_labelled, n=20)
    assert st["accuracy"]["rate"] == 1.0, st["misses"][:3]


def test_cohort_covers_every_verdict():
    verdicts = {expected_verdict(a.facts) for a in cohort(30, 7)}
    assert verdicts == {"einladen", "prüfen", "absagen"}


def test_claude_bot_request_shape():
    from screengate.claude import ClaudeBot

    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(
            stop_reason="end_turn", stop_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            content=[SimpleNamespace(type="text", text="Welche Ausbildung hast du?")])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    bot = ClaudeBot(client=client)
    msg = bot.next_message({"name": "Anna Becker", "age": 30, "job": "x"}, [])
    assert msg == "Welche Ausbildung hast du?"
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["messages"][0]["role"] == "user"
    assert kw["fallbacks"] == "default"
    assert "temperature" not in kw and "thinking" not in kw
    assert set(FIELDS)  # fields exist for the extractor schema
