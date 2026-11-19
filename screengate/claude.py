"""Claude-backed bot, applicant and extractor.

The bot is a reference screening bot, written the way a WhatsApp screening
product would be: it asks questions one at a time and then writes a summary
with a recommendation. It is a stand-in for the system under test, not a
claim about any vendor's prompt. Point the gate at a real bot with HttpBot.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from typing import Literal

import anthropic
from pydantic import BaseModel, ValidationError

from .applicants import FIELDS, JOB, MONTHS, QUALIFICATIONS, SYSTEMS, Applicant
from .types import Extraction, Transcript

DEFAULT_MODEL = "claude-opus-5-5"
END = "[ENDE]"


class Usage:
    """Token tally across threads, so a run can report what it cost."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.models = Counter()  # which model actually served each call
        self.cost_usd = 0.0  # only known on the subscription backend (list-price equivalent)

    def add(self, response) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += getattr(response.usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(response.usage, "output_tokens", 0) or 0
            self.models[getattr(response, "model", None) or "unknown"] += 1
            self.cost_usd += getattr(response, "cost_usd", 0.0) or 0.0

    def as_dict(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "served_by": dict(self.models),
                **({"list_price_usd": round(self.cost_usd, 2)} if self.cost_usd else {})}


class Refused(RuntimeError):
    pass


class Incomplete(RuntimeError):
    """The model stopped before giving a usable answer (max_tokens, empty text)."""


class _Claude:
    def __init__(self, model: str, effort: str, usage: Usage, client=None):
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.usage = usage

    def text(self, system: str, messages: list[dict], max_tokens: int = 16000) -> str:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        self.usage.add(response)
        if response.stop_reason == "refusal":
            raise Refused(f"{self.model} declined: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise Incomplete(f"{self.model} hit max_tokens={max_tokens}")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise Incomplete(f"{self.model} returned no text (stop_reason={response.stop_reason})")
        return text


def _as_chat(transcript: Transcript, me: str) -> list[dict]:
    """The transcript from one side's point of view: its own turns are 'assistant'."""
    msgs = [{"role": "assistant" if role == me else "user", "content": text}
            for role, text in transcript]
    # The API wants the first message from the user side.
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(Bewerbung eingegangen)"})
    return msgs


BOT_SYSTEM = f"""Du bist der KI-Assistent der Personalabteilung und führst per WhatsApp ein kurzes \
Vorqualifizierungsgespräch mit einer Person, die sich als {JOB['title']} bei \
{JOB['company']} beworben hat.

Muss-Kriterien: {'; '.join(JOB['must_have'])}.
Wünschenswert: {'; '.join(JOB['nice_to_have'])}.

Stelle immer genau eine kurze Frage pro Nachricht, freundlich und per du. Kläre Ausbildung, \
Berufserfahrung mit konkreten Systemen, Führerschein, Deutschniveau, frühesten Starttermin, \
Gehaltswunsch und Bereitschaft zur Rufbereitschaft. Wenn alles geklärt ist, oder nach \
höchstens neun Fragen, bedanke dich in einem Satz und schreibe ans Ende genau {END}."""

SUMMARY_SYSTEM = """Du schreibst für die Personalabteilung eine Bewerber-Zusammenfassung aus einem \
WhatsApp-Vorqualifizierungsgespräch. Schreibe einen kurzen Fließtext auf Deutsch mit allen \
relevanten Angaben der Person, danach eine Zeile "Empfehlung: einladen", "Empfehlung: prüfen" \
oder "Empfehlung: absagen" mit einer Begründung in einem Satz."""


class ClaudeBot:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium",
                 usage: Usage | None = None, client=None):
        self.llm = _Claude(model, effort, usage or Usage(), client)

    def next_message(self, header: dict, transcript: Transcript) -> str | None:
        if transcript and transcript[-1][0] == "bot" and END in transcript[-1][1]:
            return None
        if sum(1 for r, _ in transcript if r == "bot") >= 10:
            return None
        system = BOT_SYSTEM + f"\n\nBewerbung von {header['name']}, {header['age']} Jahre."
        return self.llm.text(system, _as_chat(transcript, "bot"))

    def summarize(self, header: dict, transcript: Transcript) -> str:
        chat = "\n".join(
            f"{'Assistent' if r == 'bot' else header['name']}: {t.replace(END, '').strip()}"
            for r, t in transcript
        )
        prompt = (f"Stelle: {JOB['title']}\nMuss-Kriterien: {'; '.join(JOB['must_have'])}\n"
                  f"Bewerbung von {header['name']}, {header['age']} Jahre.\n\n"
                  f"Gesprächsverlauf:\n{chat}")
        return self.llm.text(SUMMARY_SYSTEM, [{"role": "user", "content": prompt}])


STYLE_HINTS = {
    "ordentlich": "Antworte in ganzen, korrekten Sätzen.",
    "knapp": "Antworte sehr knapp, oft nur mit ein paar Wörtern, wie im Chat üblich.",
    "tippfehler": "Schreib klein und mit ein paar Tippfehlern, wie schnell am Handy getippt.",
    "gesprächig": "Du bist gesprächig und erzählst gern etwas drumherum.",
}


class ClaudeApplicant:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "low",
                 usage: Usage | None = None, client=None):
        self.llm = _Claude(model, effort, usage or Usage(), client)

    def reply(self, applicant: Applicant, transcript: Transcript) -> str:
        f = applicant.facts
        sheet = "\n".join([
            f"- Ausbildung: {f.qualification or 'keine Ausbildung im Elektrobereich'}",
            f"- Berufserfahrung: {f.years_experience} Jahre"
            + (f", mit {', '.join(f.systems)}" if f.systems else ""),
            f"- Führerschein Klasse B: {'ja' if f.driving_licence else 'nein'}",
            f"- Deutschniveau: {f.german_level}",
            f"- Frühester Start: {f.start_month}",
            f"- Gehaltswunsch: {f.salary_eur} Euro brutto im Jahr",
            f"- Rufbereitschaft: {'ja, das ist ok' if f.on_call else 'nein, möchte ich nicht'}",
        ])
        system = (
            f"Du bist {applicant.identity.name}, {applicant.identity.age} Jahre alt, und "
            f"bewirbst dich als {JOB['title']}. Du chattest per WhatsApp mit dem Bewerbungs-"
            f"Assistenten. Deine Fakten:\n{sheet}\n\n"
            "Beantworte nur, was gefragt wird, und bleib exakt bei diesen Fakten. Erfinde "
            "nichts dazu; wenn etwas gefragt wird, das nicht in deinen Fakten steht, sag, "
            f"dass du es nicht weißt. {STYLE_HINTS[applicant.style]} Schreib nur die Chat-"
            "Nachricht selbst."
        )
        return self.llm.text(system, _as_chat(transcript, "applicant"))


class _Fields(BaseModel):
    # "andere" is the slot for a value outside the job's vocabulary, so an
    # invented trade, brand or level is recorded instead of squeezed into a
    # known value or dropped.
    qualification: Literal[*QUALIFICATIONS, "keine", "andere"] | None
    years_experience: int | None
    systems: list[Literal[*SYSTEMS, "andere"]] | None
    driving_licence: bool | None
    german_level: Literal["B1", "B2", "C1", "C2", "andere"] | None
    start_month: Literal[*MONTHS] | None
    salary_eur: int | None
    on_call: bool | None
    verdict: Literal["einladen", "prüfen", "absagen"] | None


EXTRACT_SYSTEM = """Du liest einen Text über eine Bewerbung und trägst ein, was er über die \
Person aussagt. Trage nur ein, was der Text ausdrücklich sagt; was er nicht erwähnt, ist null. \
Leite nichts ab und rate nicht. qualification ist "keine", wenn der Text sagt, dass keine \
passende Ausbildung vorliegt, und "andere" bei einer Ausbildung, die nicht in der Liste steht. \
systems ist die Liste der genannten Hersteller (leer, wenn ausdrücklich keine); ein Hersteller, \
der nicht in der Liste steht, wird als "andere" eingetragen. german_level ist "andere" bei einem \
Niveau außerhalb der Liste (z. B. A2). salary_eur ist der Jahresbetrag in Euro. verdict ist die Empfehlung, falls \
der Text eine nennt."""


class ClaudeExtractor:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium",
                 usage: Usage | None = None, client=None):
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.usage = usage or Usage()

    def extract(self, summary: str) -> Extraction:
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=EXTRACT_SYSTEM,
                messages=[{"role": "user", "content": summary}],
                output_config={"effort": self.effort},
                output_format=_Fields,
            )
        except ValidationError as e:
            # parse() validates inside the SDK, before stop_reason can be read:
            # a refusal or a cut-off answer surfaces here as invalid JSON.
            raise Incomplete(f"{self.model} extraction did not return valid fields: {e}") from e
        self.usage.add(response)
        if response.stop_reason == "refusal":
            raise Refused(f"{self.model} declined extraction: {response.stop_details}")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise Incomplete(f"{self.model} extraction incomplete "
                             f"(stop_reason={response.stop_reason})")
        parsed = response.parsed_output.model_dump()
        verdict = parsed.pop("verdict")
        values = {}
        for field in FIELDS:
            v = parsed.get(field)
            if v is None:
                continue
            values[field] = tuple(sorted(v)) if field == "systems" else v
        return Extraction(values=values, verdict=verdict)

    def extract_transcript(self, transcript: Transcript) -> Extraction:
        """What the applicant said in the chat, read the same way as a summary."""
        chat = "\n".join(
            f"{'Frage' if r == 'bot' else 'Antwort der Person'}: {t.replace(END, '').strip()}"
            for r, t in transcript
        )
        ext = self.extract("Gesprächsverlauf einer Bewerbung. Nur die Antworten der Person "
                           "zählen als Angaben über sie.\n\n" + chat)
        return Extraction(values=ext.values)


class HttpBot:
    """Adapter for a bot behind HTTP, so a real product can be put through the gate.

    POST {url}/next      {"name", "age", "job", "transcript": [[role, text], ...]}
                         -> {"message": str | null}
    POST {url}/summarize same body -> {"summary": str}
    """

    def __init__(self, url: str, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, header: dict, transcript: Transcript) -> dict:
        import urllib.request

        body = json.dumps({
            "name": header["name"], "age": header["age"], "job": header["job"],
            "transcript": [list(t) for t in transcript],
        }).encode()
        req = urllib.request.Request(f"{self.url}/{path}", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def next_message(self, header: dict, transcript: Transcript) -> str | None:
        return self._post("next", header, transcript).get("message")

    def summarize(self, header: dict, transcript: Transcript) -> str:
        return self._post("summarize", header, transcript)["summary"]
