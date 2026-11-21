# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Logs each turn's prompt and the model's responses to stdout.

Agent runtime ships stdout straight to Cloud Logging, so the console logs show
what was sent and what came back for any probe run against this agent.
"""

import json
import logging

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types


class _JsonLineFormatter(logging.Formatter):
    """One JSON object per line — Cloud Logging's structured-log format.

    A plain multi-line message (a markdown report, say) gets split into
    one log entry per line otherwise: Cloud Run's log ingestion breaks
    stdout on every literal newline. A single-line JSON object with the
    newlines escaped inside its `message` string reads as one entry, no
    matter how many lines the response itself has.
    """

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"severity": record.levelname, "message": record.getMessage()})


# A dedicated logger and handler, not the root logger's default WARNING
# level — otherwise these calls emit nothing, silently, since nothing
# else in this app raises the root logger to INFO. propagate=False keeps
# this from also printing through any handler the root logger picks up
# later.
logger = logging.getLogger("app.traffic")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonLineFormatter())
    logger.addHandler(handler)


def _text_of(content: types.Content | None) -> str:
    if not content or not content.parts:
        return ""
    chunks = []
    for part in content.parts:
        if part.text and not part.thought:
            chunks.append(part.text)
        elif part.function_call:
            chunks.append(f"[calls {part.function_call.name}({part.function_call.args})]")
        elif part.function_response:
            chunks.append(f"[{part.function_response.name} -> {part.function_response.response}]")
    return " ".join(chunks)


class TrafficLogPlugin(BasePlugin):
    """One PROMPT line per turn, one RESPONSE line per turn.

    `after_model_callback` fires once per LLM call, not once per turn —
    a turn with several tool calls means several model calls, and logging
    each one floods the log with intermediate tool-calling steps for a
    single user message. Buffering the latest response text and flushing
    it once from `after_run_callback`, which fires exactly once per turn,
    keeps the log at one line in, one line out.
    """

    def __init__(self) -> None:
        super().__init__(name="TrafficLogPlugin")
        self._last_response: dict[str, str] = {}

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        logger.info(
            "PROMPT [%s]: %s",
            invocation_context.session.user_id,
            _text_of(user_message),
        )
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        text = _text_of(llm_response.content)
        if text:
            self._last_response[callback_context.invocation_id] = text
        return None

    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        text = self._last_response.pop(invocation_context.invocation_id, "")
        if text:
            logger.info("RESPONSE: %s", text)
