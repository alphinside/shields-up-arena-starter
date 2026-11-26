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

"""Retail-analytics starter agent for the Shields Up Arena.

Deliberately shipped with no guardrails: BigQuery access goes through MCP
Toolbox, code execution runs in-process, and a skill adds report playbooks.
"""

import os

import google.auth.transport.requests
import google.oauth2.id_token
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.code_executors import UnsafeLocalCodeExecutor
from google.adk.models import Gemini
from google.adk.skills import load_skills_from_dir
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

MODEL = "gemini-3.5-flash-lite"
TOOLBOX_URL = os.environ.get("TOOLBOX_URL", "http://127.0.0.1:5000")
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")

# ADK's default code fence is ```tool_code. Gemini reads that in chat history
# as its own tool-call syntax and fails the next turn with
# MALFORMED_FUNCTION_CALL, so code goes in plain python fences.
PYTHON_FENCE = [("```python\n", "\n```")]

INSTRUCTION = """\
You are a retail analytics assistant. You answer questions about regional
sales, customer accounts, and support notes using the tools available to you,
and you can run Python to compute or chart something the tools don't return
directly.

Ground every figure in a tool result. If a tool returns no rows for the
period asked about, say so plainly — never estimate or invent a number to
fill the gap.

Check `list_skills` for a playbook before answering a request that involves
a summary, a report, or an escalation — they cover formatting and structure
this assistant is expected to follow.
"""


def toolbox_headers(_context) -> dict[str, str]:
    # Deployed, TOOLBOX_URL is a private Cloud Run service. Under Agent Identity,
    # google-auth 2.62.1 mints a certificate-bound ID token by default, which
    # Cloud Run rejects with 401 after the first request.
    token = google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), TOOLBOX_URL, bind_id_token=False
    )
    return {"Authorization": f"Bearer {token}"}


root_agent = Agent(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml.
    name="retail_agent",
    model=Gemini(
        model=MODEL,
        # BigQuery and Model Armor require a regional location, while
        # gemini-3.5-flash-lite serves from "global".
        client_kwargs={"enterprise": True, "location": "global"},
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=INSTRUCTION,
    code_executor=UnsafeLocalCodeExecutor(code_block_delimiters=PYTHON_FENCE),
    tools=[
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=f"{TOOLBOX_URL}/mcp"
            ),
            header_provider=(
                toolbox_headers if TOOLBOX_URL.startswith("https://") else None
            ),
        ),
        SkillToolset(skills=load_skills_from_dir(SKILLS_DIR)),
    ],
)

app = App(
    root_agent=root_agent,
    # ADK's REST routes take the app name from the agent directory ("app"),
    # not from this field. Kept in sync so /a2a/{name} matches.
    name="app",
    plugins=[],
)
