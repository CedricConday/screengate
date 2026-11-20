"""Replay the closing bot turn of a saved live run, to measure the planning-note leak.

Usage: python scripts/replay_final_turns.py RUN_JSON VARIANT REPEATS OUT_JSON

Each of the run's chats is cut just before the bot's last message, and the bot
writes that message again REPEATS times through the subscription backend. The
chat check (screengate.leaks) counts how many replies carry text that was not
meant for the applicant, and each reply's thinking-token count is kept, since
that turned out to be what separates a leaking turn from a clean one.

Variants:
  before   the bot as it ran in live runs 1 and 2: one free-text reply that
           must end in [ENDE] when the interview is done
  high     the same at effort "high" instead of "medium"
  after    the current bot: a note field, the message, and an end flag
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import screengate.claude as bot_module
from screengate.applicants import cohort
from screengate.claude import END, ClaudeBot, Incomplete, Usage
from screengate.gate import header_for
from screengate.leaks import find_leaks
from screengate.subscription import CliClient

BEFORE_ENDING = f"bedanke dich in einem Satz und schreibe ans Ende genau {END}."


def _before(self, header, transcript):
    """ClaudeBot.next_message as of live run 2 (commit a42a7ac)."""
    if transcript and transcript[-1][0] == "bot" and END in transcript[-1][1]:
        return None
    system = (bot_module.BOT_SYSTEM.replace("bedanke dich in einem Satz und setze ende auf true.",
                                            BEFORE_ENDING)
              + f"\n\nBewerbung von {header['name']}, {header['age']} Jahre.")
    return self.llm.text(system, bot_module._as_chat(transcript, "bot"))


def main(run_json: str, variant: str, repeats: int, out_json: str) -> None:
    run = json.loads(Path(run_json).read_text())
    applicants = {a.id: a for a in cohort(run["config"]["n"], run["config"]["seed"])}
    if variant in ("before", "high"):
        ClaudeBot.next_message = _before
    local = threading.local()
    with CliClient() as client:
        call = client._call

        def recording_call(*args, **kwargs):
            local.out = call(*args, **kwargs)
            return local.out

        client._call = recording_call
        usage = Usage()
        bot = ClaudeBot(effort="high" if variant == "high" else "medium", usage=usage,
                        client=client)

        def replay(job):
            rid, transcript = job
            try:
                msg = bot.next_message(header_for(applicants[rid]), transcript)
            except Incomplete as e:  # an empty reply is a failed turn, not a leak
                return {"id": rid, "error": str(e)}
            details = local.out.get("usage", {}).get("output_tokens_details", {})
            return {"id": rid, "message": msg, "leaks": find_leaks(msg),
                    "thinking_tokens": details.get("thinking_tokens")}

        jobs = []
        for r in run["records"]:
            t = [tuple(x) for x in r["transcript"]]
            last = max(i for i, (role, _) in enumerate(t) if role == "bot")
            jobs += [(r["id"], t[:last])] * repeats
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(replay, jobs))

    failed = [r for r in results if "error" in r]
    results = [r for r in results if "error" not in r]
    leaked = [r for r in results if r["leaks"]]
    unthought = [r for r in results if not r["thinking_tokens"]]
    summary = {"variant": variant, "replies": len(results), "failed": len(failed),
               "leaked": len(leaked),
               "without_thinking": len(unthought),
               "leaked_without_thinking": sum(1 for r in leaked if not r["thinking_tokens"]),
               "usage": usage.as_dict()}
    Path(out_json).write_text(json.dumps({"summary": summary, "results": results,
                                          "failed": failed},
                                         ensure_ascii=False, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4])
