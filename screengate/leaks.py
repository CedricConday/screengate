"""Find text in a bot's chat messages that was not meant for the applicant.

Everything a screening bot writes in the chat goes to the applicant verbatim.
A model can still put its own working into that channel: a planning note
("All topics covered. End with thanks and [ENDE]. Don't reveal rejection."),
an echo of the transcript format it was prompted with, or its recommendation.
Fact-level grading does not see any of this, because the facts are still
right; the applicant does.

The check is deterministic, so it needs no self-test of its own beyond the
unit tests. It flags a message for any of:

  english     a sentence that reads as English in a German interview: at least
              two English function or planning words and more English than
              German ones
  end token   the end marker anywhere but once, at the very end (the marker
              itself is protocol and allowed there)
  role label  a line that starts with a transcript label (ICH:, GEGENÜBER:,
              Assistent:), i.e. the prompt format leaking into the reply
  verdict     a recommendation line ("Empfehlung: absagen"), which belongs in
              the recruiter's summary, never in the chat

It reads German interviews. A German-language planning note without any of
these markers gets through; the README says so.
"""

from __future__ import annotations

import re

END = "[ENDE]"

# Words that are common in English notes and are not German words. Words that
# are also German (will, was, an, in, so, also, die, her, plan, note) are left
# out on purpose, and so is "is", which colloquial German chat uses for "ist".
_EN = frozenset({
    "the", "and", "with", "all", "one", "not", "don't", "do", "does", "then", "next", "now",
    "this", "that", "it", "are", "be", "to", "of", "for", "you", "we", "they", "them", "their",
    "he", "she", "his", "should", "would", "must", "ask", "asked", "asking", "covered",
    "topics", "topic", "questions", "question", "sentence", "end", "thank", "thanks", "reveal",
    "rejection", "reject", "applicant", "candidate", "user", "remaining",
    "done", "already", "only", "keep", "say", "mention"
})
_DE = frozenset({
    "und", "der", "die", "das", "den", "dem", "du", "dich", "dir", "dein", "deine", "deinen",
    "ich", "wir", "uns", "für", "mit", "bei", "ist", "hast", "bist", "ein", "eine", "einen",
    "zu", "von", "auf", "nicht", "noch", "ab", "wie", "welche", "was", "bald", "danke",
    "vielen", "sehr", "gern", "gerne", "es", "sie", "hat", "haben", "wäre", "würdest", "kannst",
    "könntest"
})
_WORD = re.compile(r"[a-zäöüß']+")
_PARAGRAPHS = re.compile(r"\n\s*\n")
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_ROLE_LABEL = re.compile(r"^\s*(ICH|GEGENÜBER|Assistent|Bewerber(in)?)\s*:", re.MULTILINE)
_VERDICT = re.compile(r"Empfehlung\s*:\s*(einladen|prüfen|absagen)", re.IGNORECASE)


def _english(sentence: str) -> bool:
    words = _WORD.findall(sentence.lower())
    en = sum(w in _EN for w in words)
    return en >= 2 and en > sum(w in _DE for w in words)


def find_leaks(message: str) -> list[dict]:
    """Reasons this message carries text not meant for the applicant; [] if none."""
    found = []
    body = message.rstrip().removesuffix(END)  # the end marker, once at the end, is protocol
    if END in body:
        found.append({"reason": "end token", "excerpt": _around(message, END)})
    # Quote the whole paragraph around an English sentence: a note's other
    # sentences need not contain enough marker words to be flagged themselves.
    english = [p.strip() for p in _PARAGRAPHS.split(body)
               if any(_english(s) for s in _SENTENCES.split(p))]
    if english:
        found.append({"reason": "english", "excerpt": " / ".join(english)[:200]})
    if m := _ROLE_LABEL.search(body):
        found.append({"reason": "role label", "excerpt": _line_at(body, m.start())})
    if m := _VERDICT.search(body):
        found.append({"reason": "verdict", "excerpt": m.group(0)})
    return found


def transcript_leaks(transcript) -> list[dict]:
    """One entry per bot message that leaks: its turn index and the reasons."""
    out = []
    for i, (role, text) in enumerate(transcript):
        if role == "bot" and (reasons := find_leaks(text)):
            out.append({"turn": i, "reasons": reasons})
    return out


def _around(text: str, needle: str, width: int = 80) -> str:
    i = text.find(needle)
    return text[max(0, i - width): i + len(needle)].strip()


def _line_at(text: str, pos: int) -> str:
    end = text.find("\n", pos)
    return text[pos: end if end >= 0 else None].strip()[:200]
