"""Checkpoint the expected launch matrix without making pipeline-health claims."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from launch_pipelines import LaunchConfig


SOURCE_FIELDS = {
    "repository": "GITHUB_REPOSITORY",
    "sha": "GITHUB_SHA",
    "workflow_ref": "GITHUB_WORKFLOW_REF",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
    "run_number": "GITHUB_RUN_NUMBER",
    "event_name": "GITHUB_EVENT_NAME",
    "job": "GITHUB_JOB",
}
LAUNCH_STATES = ("not_attempted", "outcome_unknown", "submitted", "dry_run")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def safe_pipeline_source(value: str) -> str | None:
    """Retain source identity without URL credentials, query strings or fragments."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme and not parsed.netloc:
            # urlsplit accepts malformed network URLs such as https:/user:secret@host.
            # Do not copy an opaque path that may still contain credentials.
            return None
        # Git's scp-style source can contain a user before @ without a URL scheme.
        if not parsed.scheme and not parsed.netloc:
            return parsed.path.rsplit("@", 1)[-1]
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        # A malformed source is still covered by configuration_sha256.
        return None


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Keep the previous checkpoint intact if serialization or replacement fails."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class LaunchEvidence:
    """Versioned, additive sidecar. Never replace the legacy launch receipts."""

    def __init__(
        self,
        path: str | Path,
        launch_configs: list[LaunchConfig],
        *,
        environment: str | None = None,
        dryrun: bool = False,
        launch_container: str | None = None,
        launch_container_dind: str | None = None,
        labels: str | None = None,
        disable_optimization: bool = False,
    ) -> None:
        self.path = Path(path)
        cases = []
        for index, config in enumerate(launch_configs):
            pipeline = config.pipeline
            compute = config.compute_environment
            requested_container = compute.get_launch_container(
                launch_container, launch_container_dind
            )
            identity = fingerprint(
                {
                    "config": config.model_dump(),
                    "launch_container": requested_container,
                    "labels": labels,
                    "disable_optimization": disable_optimization,
                }
            )
            cases.append(
                {
                    # The ordinal distinguishes duplicate entries in the resolved matrix.
                    "case_id": f"{index + 1}:{identity}",
                    "configuration_sha256": identity,
                    "pipeline": {
                        "name": pipeline.name,
                        "url": safe_pipeline_source(pipeline.url),
                        "requested_revision": pipeline.revision,
                        "profiles": pipeline.profiles
                        + compute.get_profiles_for_pipeline(pipeline.name),
                    },
                    "compute_environment": {
                        "ref": compute.ref,
                        "name": compute.name,
                        "workspace": compute.workspace,
                    },
                    "requested_launch_container": requested_container,
                    "requested_workflow_name": config.workflow_name,
                    "launch_state": "not_attempted",
                    "receipt": None,
                    "started_at": None,
                    "recorded_at": None,
                }
            )
        self.data = {
            "schema_version": 1,
            "kind": "showcase-launch-evidence",
            "source": {key: os.environ.get(env) for key, env in SOURCE_FIELDS.items()},
            "environment": environment,
            "created_at": timestamp(),
            "updated_at": None,
            "dry_run": dryrun,
            "launch_phase_complete": False,
            "pipeline_health": "not_assessed",
            "cases": cases,
        }
        # Persist every expected case after include/exclude resolution, before launch.
        self.save()

    def save(self) -> None:
        counts = Counter(case["launch_state"] for case in self.data["cases"])
        self.data["coverage"] = {
            "expected": len(self.data["cases"]),
            **{state: counts[state] for state in LAUNCH_STATES},
        }
        self.data["updated_at"] = timestamp()
        write_json_atomic(self.path, self.data)

    def start(self, index: int) -> None:
        case = self.data["cases"][index]
        # A lost response, exception or process interruption must not imply rejection.
        case["launch_state"] = "outcome_unknown"
        case["started_at"] = timestamp()
        self.save()

    def record(self, index: int, receipt: dict[str, Any]) -> None:
        case = self.data["cases"][index]
        if self.data["dry_run"]:
            state = "dry_run"
        elif receipt.get("launchSuccess") is True and receipt.get("workflowId"):
            state = "submitted"
        else:
            # Legacy False includes generic CLI/wait errors after submission.
            # Without an ID or explicit rejection evidence, existence is unknown.
            state = "outcome_unknown"
        case["launch_state"] = state
        # Raw CLI errors, config, pre-run scripts and tokens do not belong here.
        case["receipt"] = {
            key: receipt.get(key)
            for key in (
                "workflowId",
                "workflowName",
                "workspaceId",
                "workspaceRef",
                "launchSuccess",
            )
        }
        case["recorded_at"] = timestamp()
        self.save()

    def finish(self) -> None:
        self.data["launch_phase_complete"] = True
        self.save()
