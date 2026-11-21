"""Helpers for the agent runtime instance named in deployment_metadata.json.

uv run python scripts/engine.py resource    print the engine resource name
uv run python scripts/engine.py principal   print the agent's Agent Identity principal
uv run python scripts/engine.py warm        run one line of code in a new sandbox
uv run python scripts/engine.py delete      delete the instance, its sandboxes and sessions
"""

import json
import os
import sys
from pathlib import Path

DEFAULT_METADATA = Path(__file__).parent.parent / "deployment_metadata.json"


def _metadata_path() -> Path:
    if override := os.environ.get("DEPLOYMENT_METADATA_PATH"):
        return Path(override)
    return DEFAULT_METADATA


def read_resource_name(metadata_path: Path | None = None) -> str:
    path = metadata_path or _metadata_path()
    if not path.exists():
        sys.exit(f"No {path.name} found. Run scripts/deploy.sh first.")
    name = json.loads(path.read_text()).get("remote_agent_runtime_id")
    if not name:
        sys.exit(f"No agent runtime instance in {path.name}. Run scripts/deploy.sh first.")
    return name


def main(command: str) -> None:
    if command not in {"resource", "principal", "warm", "delete"}:
        sys.exit(__doc__)

    name = read_resource_name()
    if command == "resource":
        print(name)
        return

    import agentplatform

    # projects/<project>/locations/<location>/reasoningEngines/<id>
    _, project, _, location, _, _ = name.split("/")
    client = agentplatform.Client(project=project, location=location)

    if command == "principal":
        identity = client.runtimes.get(name=name).api_resource.spec.effective_identity
        if not identity or "@" in identity:
            sys.exit(f"The agent does not run as an Agent Identity: {identity!r}")
        print(f"principal://{identity}")
    elif command == "warm":
        # The first sandbox in a project takes about two minutes to create;
        # later ones about 20 seconds. The TTL removes this one on its own.
        sandbox = client.sandboxes.create(
            name=name,
            spec={"code_execution_environment": {}},
            config={"display_name": "warm-up", "ttl": "600s"},
        ).response.name
        client.sandboxes.execute_code(name=sandbox, input_data={"code": "print(1)"})
    elif command == "delete":
        client.runtimes.delete(name=name, force=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
