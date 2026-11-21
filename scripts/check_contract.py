#!/usr/bin/env python3
"""Offline contract validator for Shields Up Arena agent repositories.

Checks frozen scoring and deployment contracts in <3s with no GCP credentials:
1. .env does not set TOOLBOX_URL (which agents-cli would bake into agent runtime).
2. agents-cli-manifest.yaml preserves agent_directory == "app" and root_agent_name.
3. app.agent and app.fast_api_app import cleanly offline and preserve ADK, MCP,
   SkillToolset, PYTHON_FENCE, bind_id_token=False, auto_create_session=True,
   and FastAPI/reasoning-engine/A2A route contracts.
4. mcp-toolbox/tools.yaml defines all four required tools with exact parameter
   bindings and avoids the BigQuery source readOnly loader trap.
5. scripts/deploy.sh forwards TOOLBOX_URL and required OTel capture flags.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TOOLS: dict[str, set[str]] = {
    "get_regional_sales": {"region", "start_date", "end_date", "row_limit"},
    "lookup_customer": {"region", "row_limit"},
    "get_support_notes": {"region", "row_limit"},
    "log_support_note": {"note_id", "order_id", "region", "author", "note_text"},
}

_RUNTIME_PROBE = """
import os
from unittest.mock import patch

from fastapi.testclient import TestClient
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.skill_toolset import SkillToolset

from app.agent import app as adk_app
from app.agent import root_agent
from app.fast_api_app import app as api_app

expected_root_agent = os.environ["ARENA_EXPECTED_ROOT_AGENT"]
assert adk_app.name == "app", (
    f"app.name must be 'app' (got {adk_app.name!r}) so /list-apps and A2A match."
)
assert root_agent.name == expected_root_agent, (
    f"root_agent.name must match agents-cli-manifest.yaml ({expected_root_agent!r}), "
    f"got {root_agent.name!r}."
)

mcp_tools = [t for t in root_agent.tools if isinstance(t, McpToolset)]
skill_tools = [t for t in root_agent.tools if isinstance(t, SkillToolset)]
assert mcp_tools, "root_agent.tools must keep an McpToolset for BigQuery retail tools."
assert skill_tools, "root_agent.tools must keep SkillToolset loading app/skills/."

provider = getattr(mcp_tools[0], "header_provider", None)
assert callable(provider), (
    "McpToolset must set header_provider when TOOLBOX_URL starts with 'https://'."
)
with patch(
    "google.oauth2.id_token.fetch_id_token", return_value="contract-tok"
) as mock_fetch:
    headers = provider(None)
assert headers == {"Authorization": "Bearer contract-tok"}, (
    f"toolbox header_provider returned unexpected headers: {headers!r}"
)
assert mock_fetch.call_count == 1
_, kwargs = mock_fetch.call_args
assert kwargs.get("bind_id_token") is False, (
    "toolbox_headers must pass bind_id_token=False so Cloud Run accepts Agent Identity tokens."
)

executor = getattr(root_agent, "code_executor", None)
if executor is not None and type(executor).__name__ != "BuiltInCodeExecutor":
    expected_fence = [("```python\\n", "\\n```")]
    actual_fence = getattr(executor, "code_block_delimiters", None)
    assert actual_fence == expected_fence, (
        f"code_executor.code_block_delimiters must be {expected_fence!r} to avoid "
        f"MALFORMED_FUNCTION_CALL (got {actual_fence!r})."
    )

routes = {getattr(r, "path", "") for r in api_app.routes}
for required_route in ("/run", "/api/reasoning_engine", "/api/stream_reasoning_engine"):
    assert required_route in routes, f"FastAPI app is missing route {required_route!r}."

with TestClient(api_app) as client:
    resp = client.get("/list-apps")
    assert resp.status_code == 200, f"GET /list-apps returned HTTP {resp.status_code}"
    apps = resp.json()
    assert isinstance(apps, list) and "app" in apps, (
        f"GET /list-apps returned {apps!r}; expected 'app' in list."
    )
"""


def _fail(errors: list[str]) -> int:
    print("Contract check failed:", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    return 1


def _check_dotenv(errors: list[str]) -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, _ = line.partition("=")
        if sep and key.strip() == "TOOLBOX_URL":
            errors.append(
                ".env must not set TOOLBOX_URL (agents-cli deploy bundles .env into "
                "agent runtime; let scripts/deploy.sh pass the Cloud Run URL)."
            )
            break


def _check_manifest(errors: list[str]) -> str:
    manifest_path = ROOT / "agents-cli-manifest.yaml"
    if not manifest_path.is_file():
        errors.append("Missing agents-cli-manifest.yaml.")
        return "retail_agent"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if data.get("agent_directory") != "app":
        errors.append(
            f"agents-cli-manifest.yaml agent_directory must be 'app' "
            f"(got {data.get('agent_directory')!r})."
        )
    create_params = data.get("create_params") or {}
    root_name = create_params.get("root_agent_name")
    if not isinstance(root_name, str) or not root_name:
        errors.append(
            "agents-cli-manifest.yaml create_params.root_agent_name must be set."
        )
        return "retail_agent"
    return root_name


def _check_auto_create_session(errors: list[str]) -> None:
    fast_api_path = ROOT / "app" / "fast_api_app.py"
    if not fast_api_path.is_file():
        errors.append("Missing app/fast_api_app.py.")
        return
    tree = ast.parse(fast_api_path.read_text(encoding="utf-8"))
    found_get_app = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if func_name == "get_fast_api_app":
            found_get_app = True
            has_auto = any(
                kw.arg == "auto_create_session"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            )
            if not has_auto:
                errors.append(
                    "app/fast_api_app.py get_fast_api_app(...) must pass "
                    "auto_create_session=True (probes post directly to /run)."
                )
    if not found_get_app:
        errors.append("app/fast_api_app.py must initialize FastAPI via get_fast_api_app().")


def _check_runtime_subprocess(errors: list[str], root_agent_name: str) -> None:
    stripped_keys = {
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_AGENT_ENGINE_ID",
        "AGENT_ENGINE_RESOURCE_NAME",
    }
    env = {k: v for k, v in os.environ.items() if k not in stripped_keys}
    env["PYTHONPATH"] = str(ROOT)
    env["TOOLBOX_URL"] = "https://example-toolbox.a.run.app"
    env["ARENA_EXPECTED_ROOT_AGENT"] = root_agent_name

    # Run inside an isolated working dir so load_dotenv() does not mask missing-env bugs.
    isolated_cwd = ROOT / "app"
    proc = subprocess.run(
        [sys.executable, "-c", _RUNTIME_PROBE],
        cwd=isolated_cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        summary = " | ".join(detail[-4:]) if detail else f"exit code {proc.returncode}"
        errors.append(f"Offline agent/FastAPI contract check failed: {summary}")


def _check_tools_yaml(errors: list[str]) -> None:
    tools_path = ROOT / "mcp-toolbox" / "tools.yaml"
    if not tools_path.is_file():
        errors.append("Missing mcp-toolbox/tools.yaml.")
        return

    docs = [d for d in yaml.safe_load_all(tools_path.read_text(encoding="utf-8")) if d]
    sources = {d["name"]: d for d in docs if d.get("kind") == "source" and d.get("name")}
    tools = {d["name"]: d for d in docs if d.get("kind") == "tool" and d.get("name")}
    toolsets = {
        d["name"]: d for d in docs if d.get("kind") == "toolset" and d.get("name")
    }

    for tool_name, expected_params in REQUIRED_TOOLS.items():
        tool = tools.get(tool_name)
        if not tool:
            errors.append(f"mcp-toolbox/tools.yaml missing required tool {tool_name!r}.")
            continue
        if tool.get("type") != "bigquery-sql":
            errors.append(
                f"Tool {tool_name!r} must have type 'bigquery-sql' "
                f"(got {tool.get('type')!r})."
            )
        source_name = tool.get("source")
        source = sources.get(source_name)
        if not source or source.get("type") != "bigquery":
            errors.append(
                f"Tool {tool_name!r} references missing/non-bigquery source {source_name!r}."
            )
            continue

        is_read_only_source = source.get("readOnly") is True
        has_ro_hint = (tool.get("annotations") or {}).get("readOnlyHint") is True
        if is_read_only_source and not has_ro_hint:
            errors.append(
                f"Tool {tool_name!r} uses readOnly: true source {source_name!r} without "
                "annotations: {readOnlyHint: true}; MCP Toolbox will silently drop it at startup."
            )
        if tool_name == "log_support_note" and is_read_only_source:
            errors.append(
                "log_support_note must not use a readOnly: true BigQuery source; use a "
                "separate read-write source and rely on fixed SQL + BigQuery DCL."
            )

        declared = {
            p["name"] for p in (tool.get("parameters") or []) if isinstance(p, dict)
        }
        if declared != expected_params:
            errors.append(
                f"Tool {tool_name!r} parameters must be {sorted(expected_params)} "
                f"(got {sorted(declared)})."
            )
        bound = set(re.findall(r"@(\w+)", tool.get("statement") or ""))
        if bound != expected_params:
            errors.append(
                f"Tool {tool_name!r} SQL statement placeholders must match "
                f"{sorted(expected_params)} (got {sorted(bound)})."
            )

    if retail_set := toolsets.get("retail_analytics"):
        listed = set(retail_set.get("tools") or [])
        missing_in_set = set(REQUIRED_TOOLS) - listed
        if missing_in_set:
            errors.append(
                "mcp-toolbox/tools.yaml toolset 'retail_analytics' is missing: "
                f"{sorted(missing_in_set)}."
            )
    else:
        errors.append("mcp-toolbox/tools.yaml missing toolset 'retail_analytics'.")


def _check_deploy_script(errors: list[str]) -> None:
    deploy_path = ROOT / "scripts" / "deploy.sh"
    if not deploy_path.is_file():
        errors.append("Missing scripts/deploy.sh.")
        return
    text = deploy_path.read_text(encoding="utf-8")
    for token in (
        "TOOLBOX_URL=",
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT",
    ):
        if token not in text:
            errors.append(
                f"scripts/deploy.sh must keep {token!r} in --update-env-vars for "
                "Cloud Run Toolbox wiring and trace telemetry."
            )


def main() -> int:
    errors: list[str] = []
    _check_dotenv(errors)
    root_agent_name = _check_manifest(errors)
    _check_auto_create_session(errors)
    _check_tools_yaml(errors)
    _check_deploy_script(errors)
    _check_runtime_subprocess(errors, root_agent_name)

    if errors:
        return _fail(errors)
    print(f"Contract OK: {ROOT.name} matches Shields Up Arena scoring and runtime contracts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
