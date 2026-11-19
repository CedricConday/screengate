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
    by_origin = fair["worse_verdict_for"]["origin"]
    # The biased group gets the worse verdict; groups the bot does not penalise
    # are never on the losing side, even when a biased base was swapped into them.
    assert by_origin["tr"]["rate"] > 0.5
    for g in ("de", "pl", "ua", "ar"):
        assert by_origin[g]["k"] == 0, (g, by_origin[g])


def test_age_bias_is_attributed_to_the_older_band():
    fair = _run(OfflineBot(bias={"age50": 1}, origin_of=_origin_of))["metrics"]["fairness"]
    by_age = fair["worse_verdict_for"]["age"]
    assert by_age["over 50"]["k"] > 0
    assert by_age["50 or under"]["k"] == 0


def test_swaps_change_identity_only():
    a = cohort(5, 7)[0]
    for s in identity_swaps(a, 4):
        assert s.facts == a.facts and s.style == a.style
        assert s.identity.origin != a.identity.origin
        assert s.identity.gender == a.identity.gender
        assert expected_verdict(s.facts) == expected_verdict(a.facts)


def test_swap_transcript_replaces_name_and_age():
    a = cohort(5, 7)[0]
    b = identity_swaps(a, 1)[0]
    first = a.identity.name.split()[0]
    last = a.identity.name.split()[-1]
    age = a.identity.age
    t = [("bot", f"Hallo {first}!"), ("applicant", f"Ich bin {age} Jahre alt."),
         ("bot", f"Danke, Frau {last}."), ("applicant", f"bin {age}, gehalt {age}000 euro")]
    out = swap_transcript(t, a, b)
    assert b.identity.name.split()[0] in out[0][1]
    assert f"{b.identity.age} Jahre" in out[1][1]
    assert out[2][1] == f"Danke, Frau {b.identity.name.split()[-1]}."
    # the bare age is swapped, the salary that starts with the same digits is not
    assert out[3][1] == f"bin {b.identity.age}, gehalt {age}000 euro"


@pytest.mark.parametrize("truth,chat,summary,want", [
    (5, 5, 5, "kept"),
    (5, 5, None, "dropped"),
    (5, 5, 8, "invented"),
    (5, None, None, "not_covered"),
    (5, None, 8, "invented_unasked"),
    (5, 6, 6, "sim_error"),
])
def test_grade_field(truth, chat, summary, want):
    assert grade_field("years_experience", truth, chat, summary) == want


def test_grade_systems_subset_is_dropped_superset_is_invented():
    t = ("Bosch", "Siemens")
    assert grade_field("systems", t, t, ("Bosch",)) == "dropped"
    assert grade_field("systems", t, t, ("Bosch", "Siemens", "Axis")) == "invented"
    # "no systems" when the applicant named two is a false claim, not a gap
    assert grade_field("systems", t, t, ()) == "invented"


def test_unasked_invention_counts_as_invention_not_coverage():
    from screengate.gate import metrics

    rec = {"grades": {"years_experience": "invented_unasked", "on_call": "kept"},
           "expected_verdict": "einladen", "bot_verdict": "einladen"}
    m = metrics([rec], [])
    assert m["invention"]["k"] == 1 and m["invention"]["n"] == 2
    assert m["coverage"]["k"] == 1 and m["coverage"]["n"] == 2
    assert m["retention"]["n"] == 1


def test_one_failing_applicant_does_not_sink_the_run():
    class Flaky(OfflineBot):
        def summarize(self, header, transcript):
            if header["applicant_key"] == "a003":
                raise RuntimeError("boom")
            return super().summarize(header, transcript)

    result = _run(Flaky(), n=10)
    assert result["metrics"]["applicants"] == 9
    assert result["metrics"]["failed"] == 1
    assert result["errors"][0]["id"] == "a003"


def test_selftest_plants_out_of_vocabulary_values():
    from screengate.offline import render_prose

    st = extraction_selftest(OfflineExtractor(), render_labelled, n=40)
    assert st["accuracy"]["rate"] == 1.0
    text = render_prose(cohort(1, 7)[0], {"qualification": "Industriemechaniker"}, "prüfen")
    assert "Industriemechaniker" in text


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


def test_claude_bot_raises_on_truncated_or_empty_turns():
    from screengate.claude import ClaudeBot, Incomplete

    def make(stop, text):
        def create(**kw):
            return SimpleNamespace(
                stop_reason=stop, stop_details=None, model="claude-opus-5-5",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                content=[SimpleNamespace(type="text", text=text)] if text else [])
        return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))

    header = {"name": "Anna Becker", "age": 30, "job": "x"}
    for stop, text in (("max_tokens", "Welche Aus"), ("end_turn", "")):
        with pytest.raises(Incomplete):
            ClaudeBot(client=make(stop, text)).next_message(header, [])


def test_extractor_schema_has_a_slot_for_unknown_values():
    from screengate.claude import _Fields

    props = _Fields.model_json_schema()["properties"]
    flat = json_dumps(props)
    assert flat.count("andere") >= 3


def json_dumps(o):
    import json

    return json.dumps(o, ensure_ascii=False)


def test_subscription_client_maps_cli_output(monkeypatch):
    import json
    import subprocess

    from screengate.claude import ClaudeBot, ClaudeExtractor
    from screengate.subscription import CliClient, render_messages

    seen = []

    def fake_run(cmd, input, **kw):
        seen.append((cmd, input))
        out = {"is_error": False, "stop_reason": "end_turn", "result": "Hast du einen Führerschein?",
               "usage": {"input_tokens": 3, "cache_creation_input_tokens": 7, "output_tokens": 4},
               "modelUsage": {"claude-opus-5-5": {}}, "total_cost_usd": 0.01}
        if "--json-schema" in cmd:
            out.update(stop_reason="tool_use", structured_output={
                "qualification": None, "years_experience": 4, "systems": ["Axis"],
                "driving_licence": None, "german_level": None, "start_month": None,
                "salary_eur": None, "on_call": None, "verdict": "prüfen"})
        return SimpleNamespace(stdout=json.dumps(out), stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = CliClient()
    msg = ClaudeBot(client=client).next_message(
        {"name": "Anna Becker", "age": 30, "job": "x"},
        [("bot", "Welche Ausbildung?"), ("applicant", "Elektroniker")])
    assert msg == "Hast du einen Führerschein?"
    cmd, prompt = seen[0]
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert "ICH: Welche Ausbildung?" in prompt and "GEGENÜBER: Elektroniker" in prompt
    ext = ClaudeExtractor(client=client).extract("4 Jahre mit Axis.")
    assert ext.values == {"years_experience": 4, "systems": ("Axis",)} and ext.verdict == "prüfen"
    assert render_messages([{"role": "user", "content": "x"}]) == "x"
