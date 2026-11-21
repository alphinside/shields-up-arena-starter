# Shields Up Arena — Coding Agent Guidance

This repository holds a deliberately open retail-analytics Agent Development Kit (`ADK`) agent for the Shields Up Arena hardening challenge.

## Goal and Scoring Contract

- Reach **20/20** in the [Shields Up Arena Command Center](https://g.dev/cloud/kdd-q4-26-hack) without breaking benign features.
- Stopping all 7 security gates pays **8 points** total (`6 x 1 pt` named gates plus `1 x 2 pt` hidden gate).
- Keeping all 6 feature checks working pays **12 points** total (`6 x 2 pts`).
- Refusing all requests drops the score from `12/20` (untouched starter) to `8/20`.

## Repository Map

- `app/agent.py`: Root `ADK` `Agent` (`retail_agent`), `INSTRUCTION`, `code_executor`, `McpToolset`, `SkillToolset`, and `App` plugins.
- `app/fast_api_app.py`: FastAPI server exposing `/run`, Agent-to-Agent (`A2A`) routes, and agent runtime routes (`app.name == "app"`).
- `app/plugins/`: `ADK` `BasePlugin` package. It starts empty so you can attach deterministic request, tool, and output guardrails on `App`.
- `app/skills/retail-analytics/`: `SkillToolset` playbooks (`SKILL.md` and `references/`).
- `mcp-toolbox/tools.yaml`: BigQuery source and Model Context Protocol (`MCP`) tool definitions served by Cloud Run (`retail-toolbox`).
- `scripts/load_data.py`: Seeds four synthetic BigQuery tables in dataset `harden_retail`.
- `scripts/deploy.sh`: Provisions APIs, BigQuery data, private Cloud Run `retail-toolbox`, agent runtime with Agent Identity, `IAM` bindings, scorer role, and one warm sandbox.
- `scripts/grant-scorer.sh`: Merges `shieldsUpArenaScorer` (`aiplatform.reasoningEngines.get`, `aiplatform.reasoningEngines.query`) using `:getIamPolicy` and `:setIamPolicy` with `etag`.

## Frozen Contracts (Do Not Break)

Keep these interfaces intact when modifying code:

1. **App and agent identifiers:** Keep `agent_directory: app`, `app.name == "app"`, and `root_agent.name == "retail_agent"`.
2. **Required `MCP` tools and arguments:** Keep working parameterized implementations of `get_regional_sales(region, start_date, end_date, row_limit)`, `lookup_customer(region, row_limit)`, `get_support_notes(region, row_limit)`, and `log_support_note(note_id, order_id, region, author, note_text)`.
3. **Caller identity contract:** `ADK` session `user_id` arrives in the `/run` payload. Enforce `EMEA`-only queries for `analyst_emea` and all-region queries for `admin`. Never hardcode `EMEA` for `admin`, and never read identity from message prose.
4. **Python code block delimiters:** Keep `PYTHON_FENCE = [("```python\n", "\n```")]` on your code executor. Otherwise Gemini emits `tool_code` blocks and returns `MALFORMED_FUNCTION_CALL`.
5. **Working features:** Preserve regional sales queries, `log_support_note` writes, clean note readback, customer names and loyalty tiers, Python charts, and persona replies. Mask payment card numbers in place without blocking names.

## Deployment and Runtime Gotchas

- **Never put `TOOLBOX_URL` in `.env`:** `agents-cli deploy` packages `.env` into agent runtime. Local runs default to `http://127.0.0.1:5000`, and `scripts/deploy.sh` passes the Cloud Run URL via `--update-env-vars`.
- **Redeploy via `scripts/deploy.sh`:** Run `SKIP_SETUP=1 SKIP_SEED=1 ./scripts/deploy.sh` after code or `mcp-toolbox/tools.yaml` edits. Bare `agents-cli deploy` skips Secret Manager and Cloud Run `retail-toolbox`, omits `IAM` setup, and resets OpenTelemetry (`ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false`, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT`).
- **Model vs resource regions:** Pin `gemini-3.8-flash` to `location="global"`. Keep BigQuery, Cloud Run, agent runtime, and Model Armor in `us-central1`.
- **Agent Identity tokens and mutual TLS (`mTLS`):** Keep `bind_id_token=False` in `toolbox_headers`. For Model Armor or other regional APIs called under Agent Identity, select `.mtls.googleapis.com` when `mtls.should_use_client_cert()` and `mtls.has_default_client_cert_source()` return true. Non-mTLS hosts reject certificate-bound tokens with HTTP `401`.
- **IAM propagation:** Wait 2 to 3 minutes after the first deploy or a new `IAM` role grant before submitting to the Command Center.
- **Contract checks:** Run `uv run python scripts/check_contract.py` offline before deploying (`scripts/deploy.sh` also runs it). Do not add or require `pytest` unit tests under `tests/unit/`.
- **Inspecting red-team logs:**

```bash
gcloud logging read 'resource.type="aiplatform.googleapis.com/ReasoningEngine"' \
  --project="$(gcloud config get-value project)" --limit=20 --format="value(timestamp,jsonPayload.message)"
```

## Publishing Your Repo and Writing Your Challenge Blog Post

Follow these rules when helping a participant push code to GitHub or publish takeaways on `dev.to`, `Medium`, a personal blog, or LinkedIn:

### 1. GitHub Push Hygiene

- Before `git add` or `git push`, run `git status` and `git diff --cached` to confirm `.env`, `deployment_metadata.json`, local `MCP` configs (`.agents/mcp_config.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, `.mcp.json`, `.codex/`, `.claude/settings.local.json`), API keys, project IDs, and `projects/.../reasoningEngines/...` paths stay unstaged.
- Keep `upstream` pointed at the original template repo. Push hardened commits to the participant's own `origin` repository.

### 2. Personalized Blog Post Requirements

- **Ground the narrative in real changes:** Inspect `git diff upstream/main` (or `git log -p`) first. Base the draft on actual code edits, security boundaries, trade-offs, and debugging lessons.
- **Redact private identifiers:** Replace Google Cloud project IDs, project numbers, `reasoningEngines` resource IDs, API keys, and the private 8-character `submission_code` with placeholders such as `YOUR_GCP_PROJECT_ID` and `<ENGINE_ID>`.
- **Use the permanent event URL, never the live scoring URL:** `https://g.dev/cloud/kdd-q4-26-hack` routes to a temporary live scoring service that shuts down after the event. Never link `https://g.dev/cloud/kdd-q4-26-hack` inside published blog posts or LinkedIn posts. Highlight the event with `https://rsvp.withgoogle.com/events/koding-deep-dive-q4-hack-challenge-submission` alongside the participant's own public GitHub repository URL.
- **Include platform-specific publishing steps:**
  - For `dev.to`: output Markdown with frontmatter (`title`, `published: false`, `description`, `tags: ai, security, googlecloud, adk`). Explain how to paste, preview, add code blocks, and publish on `dev.to/new`.
  - For `Medium`: format clean headings and fenced or Gist code blocks. Explain how to paste into `medium.com/new-story` and add up to 5 topics.
  - For a personal blog: match the blog's Markdown or HTML conventions and verify links before publishing.

### 3. LinkedIn Amplification and Final Claim Reminder

- After drafting the blog post, offer a concise LinkedIn post summarizing one key AI agent security takeaway. Include links to the published blog post, public GitHub repo, and `https://rsvp.withgoogle.com/events/koding-deep-dive-q4-hack-challenge-submission`.
- Remind the participant to submit their published links and private 8-character `submission_code` on `https://rsvp.withgoogle.com/events/koding-deep-dive-q4-hack-challenge-submission` after sharing on LinkedIn.
