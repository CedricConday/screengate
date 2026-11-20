"""Run the Claude roles through the `claude` CLI instead of the API.

This lets the gate run on a Claude subscription, with no API key. `CliClient`
answers the SDK calls the Claude roles make (`beta.messages.create`,
`beta.messages.parse` and `messages.parse`) by starting `claude -p` once per
call. Each call runs with a fresh session, no tools, no MCP servers and no
settings files, and with the role's own system prompt in place of Claude
Code's, so nothing but the prompt reaches the model.

The CLI takes one prompt, not a message list, so a multi-turn chat is passed as
a rendered transcript with an instruction to write the next message. This
rendering was the first suspect for the planning notes the bot sent applicants
in the second live run. Replaying the final turns did not bear that out: a
stricter instruction here left the rate where it was, and every leak came on a
turn the model answered without thinking. The fix is in the bot (claude._Turn).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Self

NEXT_TURN = ("Bisheriger Chatverlauf. Zeilen mit ICH sind deine eigenen Nachrichten, "
             "Zeilen mit GEGENÜBER die der anderen Seite.\n\n{chat}\n\n"
             "Schreibe jetzt nur deine nächste Nachricht, ohne Präfix.")


def render_messages(messages: list[dict]) -> str:
    if len(messages) == 1:
        return messages[0]["content"]
    chat = "\n".join(
        f"{'ICH' if m['role'] == 'assistant' else 'GEGENÜBER'}: {m['content']}" for m in messages
    )
    return NEXT_TURN.format(chat=chat)


class CliError(RuntimeError):
    pass


class CliClient:
    """Duck-types the parts of `anthropic.Anthropic` the Claude roles use."""

    def __init__(self, binary: str = "claude", timeout: float = 600.0):
        self.binary = shutil.which(binary) or binary
        self.timeout = timeout
        # An empty working directory, so no project CLAUDE.md is picked up.
        # Removed by close() or on leaving a with-block.
        self._workdir = tempfile.TemporaryDirectory(prefix="screengate-cli-")
        self.cwd = self._workdir.name
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create,
                                                             parse=self._parse))
        self.messages = SimpleNamespace(parse=self._parse)

    def close(self) -> None:
        self._workdir.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _call(self, model, system, prompt, effort, schema=None) -> dict:
        cmd = [self.binary, "-p", "--model", model, "--system-prompt", system,
               "--tools", "", "--strict-mcp-config", "--setting-sources", "",
               "--no-session-persistence", "--output-format", "json"]
        if effort:
            cmd += ["--effort", effort]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        proc = subprocess.run(cmd, check=False, input=prompt, capture_output=True, text=True,
                              timeout=self.timeout, cwd=self.cwd)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise CliError(f"claude -p exit {proc.returncode}: {proc.stderr[-500:]}") from e
        if out.get("is_error"):
            raise CliError(f"claude -p failed: {str(out.get('result'))[:500]}")
        return out

    @staticmethod
    def _response(out: dict, content, parsed=None):
        u = out.get("usage", {})
        served = next(iter(out.get("modelUsage") or {}), None)
        stop = out.get("stop_reason")
        if parsed is not None and stop == "tool_use":
            stop = "end_turn"  # structured output arrives as a tool call
        return SimpleNamespace(
            stop_reason=stop, stop_details=None, model=served, content=content,
            parsed_output=parsed, cost_usd=out.get("total_cost_usd") or 0.0,
            usage=SimpleNamespace(
                input_tokens=(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                              + u.get("cache_read_input_tokens", 0)),
                output_tokens=u.get("output_tokens", 0)),
        )

    def _create(self, *, model, system, messages, output_config=None, **_):
        out = self._call(model, system, render_messages(messages),
                         (output_config or {}).get("effort"))
        text = out.get("result") or ""
        return self._response(out, [SimpleNamespace(type="text", text=text)] if text else [])

    def _parse(self, *, model, system, messages, output_format, output_config=None, **_):
        out = self._call(model, system, render_messages(messages),
                         (output_config or {}).get("effort"),
                         schema=output_format.model_json_schema())
        raw = out.get("structured_output")
        # Validation errors propagate as pydantic.ValidationError, as with the SDK.
        parsed = output_format.model_validate(raw) if raw is not None else None
        return self._response(out, [], parsed)
