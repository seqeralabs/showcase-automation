#!/usr/bin/env python
"""
Launch, soak, stop and delete Studios on the Seqera Platform.

Two sub-commands mirror the pipeline scripts in this repository
(`launch_pipelines.py` and `extract_metadata.py`):

    launch  Reads Studio definitions from YAML files, creates one Studio per
            entry (auto-started), waits until it reaches the `running` status
            (or fails / times out) and writes the launch records to a JSON file.
            Failures to create or start a Studio are logged, not raised, so a
            single broken Studio does not abort the rest.
    finish  Reads the launch records, checks that every Studio is still
            `running` after the soak period, stops it, waits until it is
            `stopped`, optionally deletes it and writes the results to a JSON
            file (and to the GitHub Actions job summary when available).

Usage:
    uv run --locked python studios_autotest.py launch -i studios/staging*.yaml -o launch.json
    uv run --locked python studios_autotest.py finish -i launch.json -o results.json --delete

Studios are driven through the `tw` CLI (via seqerakit), so `TOWER_ACCESS_TOKEN`
and, for non-production environments, `TOWER_API_ENDPOINT` must be set.
"""

import argparse
import datetime
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pydantic
import yaml
from seqerakit import seqeraplatform

## Globals
# Global UUID and date shared by every Studio name created in this run,
# mirroring the naming scheme used by launch_pipelines.py.
run_uuid = str(uuid.uuid4()).replace("-", "")[:15]
date = datetime.datetime.now().strftime("%Y%m%d")

# Studio statuses as returned by the Seqera Platform API (DataStudioStatus).
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
TRANSIENT_STATUSES = {"starting", "building", "stopping"}
STARTUP_FAILURE_STATUSES = {"errored", "buildFailed", "stopped"}
STOP_TERMINAL_STATUSES = {"stopped", "errored"}
DELETABLE_STATUSES = {"stopped", "errored", "buildFailed"}
STATUS_TIMEOUT = "TIMEOUT"

# Outcome of a single Studio test, in the order they can occur.
OUTCOME_PASSED = "PASSED"
OUTCOME_DRYRUN = "DRYRUN"
OUTCOME_FAILED_TO_CREATE = "FAILED_TO_CREATE"
OUTCOME_FAILED_TO_START = "FAILED_TO_START"
OUTCOME_FAILED_SOAK = "FAILED_SOAK"
OUTCOME_FAILED_TO_STOP = "FAILED_TO_STOP"

# Errors raised by seqerakit when a `tw` command exits non-zero.
TW_ERRORS = (
    seqeraplatform.CommandError,
    seqeraplatform.ResourceExistsError,
    seqeraplatform.ResourceNotFoundError,
)

# Template status preference when resolving an unpinned template.
# `unsupported` templates are never selected.
TEMPLATE_STATUS_RANK = {"recommended": 3, "experimental": 2, None: 1, "deprecated": 0}
TEMPLATE_TAG_RE = re.compile(
    r"^(?P<tool>\d+(?:\.\d+)*)(?:-u(?P<update>\d+))?-(?P<connect>\d+(?:\.\d+)*)$"
)


class SeqeraKitError(Exception):
    """Exception for unexpected output from the Tower CLI."""


class TemplateNotFoundError(Exception):
    """Exception for a Studio template that is not available in the workspace."""


class StudioSpec(pydantic.BaseModel):
    """A Studio to create, as declared in the `studios` YAML files."""

    ref: str
    name: str
    workspace: str
    compute_env: str
    template: str
    description: str | None = None
    cpu: int = 2
    memory: int = 8192
    gpu: int = 0
    lifespan: int | None = None
    mount_data_uris: list[str] = []
    private: bool = False


class StudioLaunch(pydantic.BaseModel):
    """The record written by `launch` for each Studio and read back by `finish`."""

    ref: str
    studioName: str
    workspaceId: str
    workspaceRef: str | None = None
    computeEnvironment: str
    template: str
    sessionId: str | None = None
    studioUrl: str | None = None
    launchSuccess: bool = False
    startupStatus: str | None = None
    startupSeconds: float | None = None
    statusMessage: str | None = None
    error: str = ""
    dryrun: bool = False


class StudioResult(StudioLaunch):
    """The launch record enriched by `finish` with the soak, stop and delete results."""

    checkStatus: str | None = None
    checkMessage: str | None = None
    stopStatus: str | None = None
    deleted: bool = False
    outcome: str = OUTCOME_PASSED
    outcomeDetail: str = ""


class StatusObservation(pydantic.BaseModel):
    """The status of a Studio observed while waiting for a condition."""

    status: str | None
    message: str | None = None
    stopReason: str | None = None
    elapsedSeconds: float
    timedOut: bool = False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse command line arguments.

    Args:
        argv (list[str], optional): Arguments to parse instead of sys.argv.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-l",
        "--log_level",
        default="INFO",
        choices=("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"),
        help="The desired log level (default: INFO).",
        type=str.upper,
    )
    common.add_argument(
        "--poll-interval",
        type=float,
        default=30,
        help="Seconds between Studio status checks (default: 30).",
    )

    parser = argparse.ArgumentParser(
        description="Create, soak, stop and delete Studios on the Seqera Platform."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch = subparsers.add_parser(
        "launch",
        parents=[common],
        help="Create the Studios and wait until they are running.",
    )
    launch.add_argument(
        "-i",
        "--inputs",
        nargs="+",
        required=True,
        type=Path,
        help="The input YAML files to read. Must contain the key 'studios'.",
    )
    launch.add_argument(
        "-o", "--output", type=str, required=True, help="Output filename for JSON file."
    )
    launch.add_argument(
        "--labels",
        type=str,
        help="Comma-separated resource labels (key=value) to add to every Studio.",
        required=False,
    )
    launch.add_argument(
        "--template",
        type=str,
        help="Override the template of every Studio (repository[:tag]).",
        required=False,
    )
    launch.add_argument(
        "--startup-timeout",
        type=float,
        default=30,
        help="Minutes to wait for a Studio to reach the running status (default: 30).",
    )
    launch.add_argument(
        "-d",
        "--dryrun",
        action="store_true",
        help="Dry run the Studio creation without actually creating anything.",
    )

    finish = subparsers.add_parser(
        "finish",
        parents=[common],
        help="Check, stop, delete and report the Studios created by 'launch'.",
    )
    finish.add_argument(
        "-i",
        "--input",
        type=str,
        required=True,
        nargs="+",
        help="JSON file(s) written by the 'launch' sub-command.",
    )
    finish.add_argument(
        "-o", "--output", type=str, required=True, help="Output filename for JSON file."
    )
    finish.add_argument(
        "--delete",
        action="store_true",
        help="Delete each Studio once it is stopped (or errored).",
    )
    finish.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Attempt to delete each Studio whatever its status.",
    )
    finish.add_argument(
        "--stop-timeout",
        type=float,
        default=20,
        help="Minutes to wait for a Studio to stop (default: 20).",
    )
    finish.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit with a non-zero code if any Studio did not pass.",
    )
    return parser.parse_args(argv)


def read_yaml(paths: list[Path]) -> list[StudioSpec]:
    """
    Read one or more YAML files containing a `studios` list.

    Args:
        paths (list[Path]): The paths to the YAML files.

    Returns:
        list[StudioSpec]: The Studios declared in the files, in order.
    """
    logging.info("Reading studio details...")
    entries: list[dict[str, Any]] = []
    for path in paths:
        with open(path) as studio_file:
            file_contents = yaml.safe_load(studio_file) or {}
        for key, value in file_contents.items():
            if key != "studios":
                raise KeyError(f"Unexpected key in YAML file {path}: {key}")
            entries.extend(value or [])
    return [StudioSpec(**entry) for entry in entries]


def sanitize_name_part(value: str) -> str:
    """Keep only characters that are safe in a Studio name."""
    return re.sub(r"[^A-Za-z0-9-]+", "-", value).strip("-")


def studio_name(spec: StudioSpec) -> str:
    """
    Build a unique Studio name from the spec, the date and the run UUID.

    Args:
        spec (StudioSpec): The Studio to name.

    Returns:
        str: A name such as `jupyter-aws-cloud-20250101-0123456789abcde`.
    """
    parts = [
        sanitize_name_part(spec.name),
        sanitize_name_part(spec.ref),
        date,
        run_uuid,
    ]
    return "-".join(part for part in parts if part)


def studio_description(spec: StudioSpec) -> str:
    """Build the Studio description, adding a link to the GitHub Actions run when available."""
    description = (
        spec.description or "Automated Studio test created by showcase-automation"
    )
    run_id = os.environ.get("GITHUB_RUN_ID")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if run_id and repository:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        description = f"{description} - {server}/{repository}/actions/runs/{run_id}"
    return description


def split_template(template: str) -> tuple[str, str | None]:
    """
    Split a template reference into repository and tag.

    Args:
        template (str): A container reference such as `registry/repo:tag`.

    Returns:
        tuple[str, str | None]: The repository and the tag (None when unpinned).
    """
    repository, separator, tag = template.rpartition(":")
    # No colon at all, or the colon belongs to a registry port (`host:5000/repo`).
    if not separator or "/" in tag:
        return template, None
    return repository, tag


def parse_template_tag(tag: str) -> tuple:
    """
    Build a sort key for a Studio template tag of the form `<tool>-[u<n>-]<connect>`.

    Tags that do not follow the convention sort lowest.

    Args:
        tag (str): The image tag, e.g. `4.6.0-0.12` or `4.4.1-u1-0.7`.

    Returns:
        tuple: A key that sorts newer versions higher.
    """
    match = TEMPLATE_TAG_RE.match(tag)
    if match is None:
        return (0, (), 0, ())

    def version(text: str) -> tuple[int, ...]:
        return tuple(int(part) for part in text.split("."))

    return (
        1,
        version(match["tool"]),
        int(match["update"] or 0),
        version(match["connect"]),
    )


def resolve_template(template: str, available: list[dict[str, Any]]) -> str:
    """
    Resolve an unpinned template repository to the best available template.

    A template with a tag is returned unchanged. Otherwise the available
    templates for the same repository are ranked by status (recommended first,
    unsupported excluded) and then by version, and the best one is returned.

    Args:
        template (str): The repository, optionally with a tag.
        available (list[dict]): Templates returned by `tw studios templates`.

    Raises:
        TemplateNotFoundError: If no usable template matches the repository.

    Returns:
        str: The fully tagged template reference.
    """
    repository, tag = split_template(template)
    if tag is not None:
        return template

    candidates = [
        candidate
        for candidate in available
        if candidate.get("repository")
        and split_template(candidate["repository"])[0] == repository
        and candidate.get("status") != "unsupported"
    ]
    if not candidates:
        repositories = sorted(
            {
                split_template(t["repository"])[0]
                for t in available
                if t.get("repository")
            }
        )
        raise TemplateNotFoundError(
            f"No usable Studio template found for '{repository}'. "
            f"Available repositories: {', '.join(repositories) or 'none'}"
        )

    def rank(candidate: dict[str, Any]) -> tuple:
        status_rank = TEMPLATE_STATUS_RANK.get(candidate.get("status"), 1)
        _, candidate_tag = split_template(candidate["repository"])
        return (status_rank, parse_template_tag(candidate_tag or ""))

    best = max(candidates, key=rank)
    return best["repository"]


def tw_studios(
    seqera: seqeraplatform.SeqeraPlatform, *args: str, quiet: bool = False
) -> dict[str, Any] | None:
    """
    Run `tw -o json studios <args>` and return the parsed JSON response.

    Args:
        seqera (SeqeraPlatform): The seqerakit wrapper around the `tw` CLI.
        *args (str): The `studios` sub-command and its options.
        quiet (bool): Do not echo the JSON response to stdout.

    Raises:
        SeqeraKitError: If the CLI returned something that is not JSON.
        CommandError: If the CLI exited with an error (raised by seqerakit).

    Returns:
        dict | None: The parsed JSON, or None when running in dryrun mode.
    """
    if quiet:
        with seqera.suppress_output():
            result = seqera.studios(*args)
    else:
        result = seqera.studios(*args)

    if seqera.dryrun:
        return None
    if not isinstance(result, dict):
        raise SeqeraKitError(f"Unexpected output from 'tw studios {args[0]}': {result}")
    return result


def list_templates(
    seqera: seqeraplatform.SeqeraPlatform, workspace: str
) -> list[dict[str, Any]]:
    """
    List the Studio templates available in a workspace.

    `tw studios add` validates the template against the first 20 templates of
    the workspace, so the same page size is used here to resolve from that set.
    """
    response = tw_studios(
        seqera, "templates", "-w", workspace, "--max", "20", quiet=True
    )
    return list((response or {}).get("templates") or [])


def describe_studio(
    seqera: seqeraplatform.SeqeraPlatform, session_id: str, workspace: str
) -> dict[str, Any]:
    """Return the Studio DTO for a session ID."""
    response = tw_studios(seqera, "view", "-i", session_id, "-w", workspace, quiet=True)
    return dict((response or {}).get("studio") or {})


def studio_status(studio: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Extract (status, message, stopReason) from a Studio DTO."""
    info = studio.get("statusInfo") or {}
    return info.get("status"), info.get("message"), info.get("stopReason")


def studio_progress(studio: dict[str, Any]) -> str:
    """Return the message of the progress step currently in progress or errored."""
    for step in studio.get("progress") or []:
        if step.get("status") in ("in-progress", "errored"):
            return str(step.get("message") or "")
    return ""


def wait_for_status(
    seqera: seqeraplatform.SeqeraPlatform,
    session_id: str,
    workspace: str,
    until: Callable[[str | None], bool],
    timeout_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StatusObservation:
    """
    Poll a Studio until its status satisfies `until` or the timeout expires.

    Transient CLI errors while polling are logged and retried until the timeout.

    Args:
        seqera (SeqeraPlatform): The seqerakit wrapper around the `tw` CLI.
        session_id (str): The Studio session ID.
        workspace (str): The workspace ID.
        until (Callable): Returns True when the observed status is final.
        timeout_seconds (float): Maximum time to wait.
        poll_seconds (float): Delay between polls.
        clock (Callable): Monotonic clock, injectable for tests.
        sleep (Callable): Sleep function, injectable for tests.

    Returns:
        StatusObservation: The last observed status and whether we timed out.
    """
    start = clock()
    status: str | None = None
    message: str | None = None
    stop_reason: str | None = None
    while True:
        try:
            studio = describe_studio(seqera, session_id, workspace)
            status, message, stop_reason = studio_status(studio)
            progress = studio_progress(studio)
            logging.info(
                f"Studio {session_id} status: {status}"
                + (f" ({progress})" if progress else "")
            )
            if until(status):
                return StatusObservation(
                    status=status,
                    message=message,
                    stopReason=stop_reason,
                    elapsedSeconds=clock() - start,
                )
        except TW_ERRORS as err:
            logging.warning(f"Could not fetch status of studio {session_id}: {err}")

        elapsed = clock() - start
        if elapsed >= timeout_seconds:
            logging.warning(
                f"Timed out after {elapsed:.0f}s waiting for studio {session_id} (last status: {status})"
            )
            return StatusObservation(
                status=status,
                message=message,
                stopReason=stop_reason,
                elapsedSeconds=elapsed,
                timedOut=True,
            )
        sleep(poll_seconds)


def build_add_args(
    spec: StudioSpec,
    name: str,
    template: str,
    labels: str | None = None,
    description: str | None = None,
) -> list[str]:
    """
    Build the arguments of `tw studios add` for a Studio spec.

    Args:
        spec (StudioSpec): The Studio to create.
        name (str): The unique Studio name.
        template (str): The fully tagged template reference.
        labels (str, optional): Comma-separated resource labels (key=value).
        description (str, optional): The Studio description.

    Returns:
        list[str]: The `tw studios` sub-command and options.
    """
    args = [
        "add",
        "-n",
        name,
        "-w",
        spec.workspace,
        "-c",
        spec.compute_env,
        "-t",
        template,
        "--cpu",
        str(spec.cpu),
        "--memory",
        str(spec.memory),
        "--gpu",
        str(spec.gpu),
        "--auto-start",
    ]
    if spec.lifespan is not None:
        args.extend(["--lifespan", str(spec.lifespan)])
    if spec.mount_data_uris:
        args.extend(["--mount-data-uris", ",".join(spec.mount_data_uris)])
    if spec.private:
        args.append("--private")
    if description:
        args.extend(["-d", description])
    if labels:
        args.extend(["--labels", labels])
    return args


def launch_studio(
    seqera: seqeraplatform.SeqeraPlatform,
    spec: StudioSpec,
    labels: str | None,
    startup_timeout_seconds: float,
    poll_seconds: float,
    templates_cache: dict[str, list[dict[str, Any]]],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StudioLaunch:
    """
    Create a Studio, start it and wait until it is running.

    Failures are recorded in the returned record instead of being raised.

    Args:
        seqera (SeqeraPlatform): The seqerakit wrapper around the `tw` CLI.
        spec (StudioSpec): The Studio to create.
        labels (str, optional): Comma-separated resource labels (key=value).
        startup_timeout_seconds (float): Maximum time to wait for `running`.
        poll_seconds (float): Delay between status polls.
        templates_cache (dict): Templates already listed, keyed by workspace.

    Returns:
        StudioLaunch: The launch record.
    """
    name = studio_name(spec)
    record = StudioLaunch(
        ref=spec.ref,
        studioName=name,
        workspaceId=spec.workspace,
        computeEnvironment=spec.compute_env,
        template=spec.template,
        dryrun=seqera.dryrun,
    )
    logging.info(f"Creating studio {name} on {spec.compute_env} ({spec.ref}).")

    try:
        if seqera.dryrun and split_template(spec.template)[1] is None:
            logging.info(f"DRYRUN: skipping template resolution for {spec.template}")
            template = spec.template
        else:
            if spec.workspace not in templates_cache:
                templates_cache[spec.workspace] = list_templates(seqera, spec.workspace)
            template = resolve_template(spec.template, templates_cache[spec.workspace])
        record.template = template

        created = tw_studios(
            seqera,
            *build_add_args(spec, name, template, labels, studio_description(spec)),
        )
        if seqera.dryrun:
            record.startupStatus = OUTCOME_DRYRUN
            return record
        assert created is not None
        record.sessionId = created["sessionId"]
        record.studioUrl = created.get("studioUrl")
        record.workspaceRef = created.get("workspaceRef")
        record.launchSuccess = True
    # Predictable failures are logged so the remaining Studios are still launched.
    except (*TW_ERRORS, TemplateNotFoundError, SeqeraKitError, KeyError) as err:
        message = "\n".join(str(arg) for arg in err.args)
        logging.info(f"Failed to create studio {name}. Logging and proceeding...")
        logging.debug(message)
        record.error = message
        return record

    observation = wait_for_status(
        seqera,
        record.sessionId,
        spec.workspace,
        until=lambda status: (
            status == STATUS_RUNNING or status in STARTUP_FAILURE_STATUSES
        ),
        timeout_seconds=startup_timeout_seconds,
        poll_seconds=poll_seconds,
        clock=clock,
        sleep=sleep,
    )
    record.startupStatus = (
        STATUS_TIMEOUT if observation.timedOut else observation.status
    )
    record.startupSeconds = round(observation.elapsedSeconds, 1)
    record.statusMessage = observation.message
    if record.startupStatus == STATUS_RUNNING:
        logging.info(f"Studio {name} is running after {record.startupSeconds}s.")
    else:
        logging.warning(
            f"Studio {name} did not reach running: {record.startupStatus} ({observation.message})"
        )
    return record


def launch_studios(
    seqera: seqeraplatform.SeqeraPlatform,
    specs: list[StudioSpec],
    records: list[StudioLaunch],
    labels: str | None,
    startup_timeout_seconds: float,
    poll_seconds: float,
) -> None:
    """
    Launch every Studio spec in turn, appending each record to `records`.

    The list is filled incrementally so the caller can persist partial results
    if an unexpected error interrupts the loop.
    """
    logging.info("Launching studios.")
    templates_cache: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        records.append(
            launch_studio(
                seqera,
                spec,
                labels,
                startup_timeout_seconds,
                poll_seconds,
                templates_cache,
            )
        )
    logging.info("Studios launched.")


def finish_studio(
    seqera: seqeraplatform.SeqeraPlatform,
    launch: StudioLaunch,
    delete: bool,
    force: bool,
    stop_timeout_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StudioResult:
    """
    Check a Studio after the soak period, stop it, delete it and classify the outcome.

    The first failure encountered decides the outcome; every detail is kept in
    `outcomeDetail`. Cleanup is always attempted so no Studio is left running.

    Args:
        seqera (SeqeraPlatform): The seqerakit wrapper around the `tw` CLI.
        launch (StudioLaunch): The record written by `launch`.
        delete (bool): Delete the Studio once it is stopped or errored.
        force (bool): Attempt the deletion whatever the status.
        stop_timeout_seconds (float): Maximum time to wait for `stopped`.
        poll_seconds (float): Delay between status polls.

    Returns:
        StudioResult: The launch record enriched with the final results.
    """
    result = StudioResult(**launch.model_dump())
    details: list[str] = []

    def fail(outcome: str, detail: str) -> None:
        if result.outcome == OUTCOME_PASSED:
            result.outcome = outcome
        if detail:
            details.append(detail)

    if launch.dryrun:
        result.outcome = OUTCOME_DRYRUN
        return result

    if not launch.sessionId:
        fail(OUTCOME_FAILED_TO_CREATE, launch.error.strip() or "no session ID recorded")
        result.outcomeDetail = "; ".join(details)
        return result

    session_id, workspace = launch.sessionId, launch.workspaceId
    if launch.startupStatus != STATUS_RUNNING:
        fail(
            OUTCOME_FAILED_TO_START,
            f"startup ended with status '{launch.startupStatus}'"
            + (f": {launch.statusMessage}" if launch.statusMessage else ""),
        )

    # 1. Soak check: the Studio must still be running after the wait period.
    try:
        studio = describe_studio(seqera, session_id, workspace)
    except TW_ERRORS as err:
        fail(OUTCOME_FAILED_SOAK, f"could not describe studio: {err}")
        result.outcomeDetail = "; ".join(details)
        return result
    status, message, stop_reason = studio_status(studio)
    result.checkStatus, result.checkMessage = status, message
    logging.info(f"Studio {launch.studioName} is '{status}' after the soak period.")

    if status in TRANSIENT_STATUSES:
        observation = wait_for_status(
            seqera,
            session_id,
            workspace,
            until=lambda observed: (
                observed is not None and observed not in TRANSIENT_STATUSES
            ),
            timeout_seconds=stop_timeout_seconds,
            poll_seconds=poll_seconds,
            clock=clock,
            sleep=sleep,
        )
        status, message, stop_reason = (
            observation.status,
            observation.message,
            observation.stopReason,
        )

    if launch.startupStatus == STATUS_RUNNING and status != STATUS_RUNNING:
        reason = stop_reason or message or "no message"
        fail(
            OUTCOME_FAILED_SOAK,
            f"studio was '{status}' after the soak period ({reason})",
        )

    # 2. Stop the Studio if it is running.
    if status == STATUS_RUNNING:
        logging.info(f"Stopping studio {launch.studioName}.")
        try:
            tw_studios(seqera, "stop", "-i", session_id, "-w", workspace)
            observation = wait_for_status(
                seqera,
                session_id,
                workspace,
                until=lambda observed: observed in STOP_TERMINAL_STATUSES,
                timeout_seconds=stop_timeout_seconds,
                poll_seconds=poll_seconds,
                clock=clock,
                sleep=sleep,
            )
            status = observation.status
            result.stopStatus = STATUS_TIMEOUT if observation.timedOut else status
            if status != STATUS_STOPPED:
                fail(
                    OUTCOME_FAILED_TO_STOP,
                    f"stop ended with status '{result.stopStatus}'"
                    + (f": {observation.message}" if observation.message else ""),
                )
        except TW_ERRORS as err:
            result.stopStatus = "ERROR"
            fail(OUTCOME_FAILED_TO_STOP, f"stop failed: {err}")
    elif status in TRANSIENT_STATUSES:
        fail(
            OUTCOME_FAILED_TO_STOP,
            f"studio still '{status}' after {stop_timeout_seconds:.0f}s",
        )

    # 3. Delete the Studio so nothing is left behind.
    if delete:
        if status in DELETABLE_STATUSES or force:
            logging.info(f"Deleting studio {launch.studioName}.")
            try:
                tw_studios(seqera, "delete", "-i", session_id, "-w", workspace)
                result.deleted = True
            except TW_ERRORS as err:
                logging.error(f"Error deleting studio {launch.studioName}: {err}")
                details.append(f"delete failed: {err}")
        else:
            details.append(
                f"not deleted: studio is '{status}' (use --force to delete anyway)"
            )

    result.outcomeDetail = "; ".join(details)
    return result


def read_launch_records(paths: list[str]) -> list[StudioLaunch]:
    """Read the JSON list(s) written by `launch`."""
    records: list[StudioLaunch] = []
    for path in paths:
        with open(path) as infile:
            # Be aware this is expecting a list of studios in the JSON file
            records.extend(
                StudioLaunch.model_validate(item) for item in json.load(infile)
            )
    return records


def write_json(path: str, models: list[pydantic.BaseModel]) -> None:
    """Write a list of pydantic models to a JSON file."""
    logging.info(f"Writing {len(models)} record(s) to JSON file {path}")
    with open(path, "w") as output_file:
        json.dump([model.model_dump() for model in models], output_file, indent=4)


def get_status_emoji(status: str | None) -> str:
    """Get an emoji representation for a Studio status."""
    status_map = {
        "running": "✅",
        "stopped": "⏸️",
        "starting": "🚀",
        "stopping": "⏳",
        "building": "🔨",
        "errored": "❌",
        "buildFailed": "❌",
        STATUS_TIMEOUT: "⏰",
        "ERROR": "❌",
    }
    return status_map.get(status or "", "❓")


def get_outcome_emoji(outcome: str) -> str:
    """Get an emoji representation for a Studio outcome."""
    if outcome == OUTCOME_PASSED:
        return "✅"
    if outcome == OUTCOME_DRYRUN:
        return "📝"
    return "❌"


def sort_results(results: list[StudioResult]) -> list[StudioResult]:
    """Sort results with failures first, then by Studio name."""
    return sorted(
        results,
        key=lambda result: (
            0 if result.outcome.startswith("FAILED") else 1,
            result.studioName.lower(),
        ),
    )


def build_summary(results: list[StudioResult]) -> dict[str, int]:
    """Count passed, failed and other outcomes."""
    summary = {"total": len(results), "passed": 0, "failed": 0, "other": 0}
    for result in results:
        if result.outcome == OUTCOME_PASSED:
            summary["passed"] += 1
        elif result.outcome.startswith("FAILED"):
            summary["failed"] += 1
        else:
            summary["other"] += 1
    return summary


def format_startup(result: StudioResult) -> str:
    """Format the startup column, e.g. `✅ running (312s)`."""
    if not result.startupStatus:
        return "-"
    text = f"{get_status_emoji(result.startupStatus)} {result.startupStatus}"
    if result.startupSeconds is not None:
        text += f" ({result.startupSeconds:.0f}s)"
    return text


def markdown_cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", "<br>")


def build_markdown_summary(results: list[StudioResult]) -> str:
    """
    Build a Markdown report with one row per Studio, failures first.

    Args:
        results (list[StudioResult]): The Studio results.

    Returns:
        str: Markdown suitable for the GitHub Actions job summary.
    """
    summary = build_summary(results)
    lines = [
        "## Studios report",
        "",
        f"{summary['total']} studios: {summary['passed']} passed, "
        f"{summary['failed']} failed, {summary['other']} other",
        "",
        "| Studio | Compute environment | Template | Startup | After soak | Stop | Deleted | Outcome |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in sort_results(results):
        studio = result.studioName
        if result.studioUrl and not result.deleted:
            studio = f"[{studio}]({result.studioUrl})"
        check = (
            f"{get_status_emoji(result.checkStatus)} {result.checkStatus}"
            if result.checkStatus
            else "-"
        )
        stop = (
            f"{get_status_emoji(result.stopStatus)} {result.stopStatus}"
            if result.stopStatus
            else "-"
        )
        outcome = f"{get_outcome_emoji(result.outcome)} {result.outcome}"
        if result.outcomeDetail:
            outcome += f"<br>{result.outcomeDetail}"
        cells = [
            studio,
            result.computeEnvironment,
            result.template.rsplit("/", 1)[-1],
            format_startup(result),
            check,
            stop,
            "yes" if result.deleted else "⚠️ no",
            outcome,
        ]
        lines.append("| " + " | ".join(markdown_cell(cell) for cell in cells) + " |")
    return "\n".join(lines) + "\n"


def write_step_summary(results: list[StudioResult]) -> None:
    """Append the Markdown report to the GitHub Actions job summary, if running in Actions."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a") as summary_file:
        summary_file.write(build_markdown_summary(results))


def main_launch(args: argparse.Namespace) -> int:
    """Run the `launch` sub-command."""
    seqera = seqeraplatform.SeqeraPlatform(dryrun=args.dryrun, json=True)

    specs = read_yaml(args.inputs)
    if args.template:
        for spec in specs:
            spec.template = args.template

    records: list[StudioLaunch] = []
    try:
        launch_studios(
            seqera,
            specs,
            records,
            labels=args.labels,
            startup_timeout_seconds=args.startup_timeout * 60,
            poll_seconds=args.poll_interval,
        )
    finally:
        # Always persist what was created so `finish` can clean it up.
        write_json(args.output, records)
    return 0


def main_finish(args: argparse.Namespace) -> int:
    """Run the `finish` sub-command."""
    seqera = seqeraplatform.SeqeraPlatform(json=True)

    logging.info("Reading studio details from JSON file...")
    launches = read_launch_records(args.input)

    results = [
        finish_studio(
            seqera,
            launch,
            delete=args.delete,
            force=args.force,
            stop_timeout_seconds=args.stop_timeout * 60,
            poll_seconds=args.poll_interval,
        )
        for launch in launches
    ]
    write_json(args.output, results)

    summary = build_summary(results)
    logging.info(
        f"Studios Report: {summary['total']} studios "
        f"({summary['passed']} passed, {summary['failed']} failed, {summary['other']} other)"
    )
    for result in results:
        logging.info(f"  {result.studioName}: {result.outcome} {result.outcomeDetail}")

    write_step_summary(results)

    if args.fail_on_error and summary["failed"] > 0:
        logging.error(f"{summary['failed']} studio(s) failed.")
        return 1
    return 0


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=args.log_level)
    if args.command == "launch":
        raise SystemExit(main_launch(args))
    raise SystemExit(main_finish(args))


if __name__ == "__main__":
    main()
