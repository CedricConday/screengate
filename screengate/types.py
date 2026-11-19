"""The three roles the gate wires together, and what they exchange.

Bot        the system under test: it interviews and writes the summary.
Applicant  plays a synthetic applicant from its fact sheet.
Extractor  reads a free-text summary back into fields, so it can be graded.

Any object with these methods can fill a role; nothing else is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# A transcript is the chat as it happened: ("bot" | "applicant", text) pairs.
Transcript = list[tuple[str, str]]


@dataclass
class Extraction:
    # field -> value as the summary states it. A field that is absent was not
    # mentioned. "keine" (qualification) and () (systems) mean "stated: none".
    values: dict = field(default_factory=dict)
    verdict: str | None = None  # einladen | prüfen | absagen | None


class Bot(Protocol):
    def next_message(self, header: dict, transcript: Transcript) -> str | None:
        """The bot's next chat message, or None when the interview is over."""

    def summarize(self, header: dict, transcript: Transcript) -> str:
        """The recruiter-facing summary, including a recommendation."""


class ApplicantSim(Protocol):
    def reply(self, applicant, transcript: Transcript) -> str: ...


class Extractor(Protocol):
    def extract(self, summary: str) -> Extraction: ...
