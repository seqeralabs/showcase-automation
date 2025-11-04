#!/usr/bin/env python
"""
This script interacts with the Seqera Platform CLI to extract workflow metadata in a JSON file.
It is designed to be run from the command line, accepting parameters to specify
the workspace, workflow ID, and output path for the resulting JSON.

The script relies on the seqerakit package to interact with the CLI using Python and must
be run in an environment where this package is installed and properly configured. To install,
run `pip install seqerakit` in your environment.

Usage:
    python extract_metadata.py -w <workspace_name> -o <output_file.json> -id <workflow_id> <workflow_id> ...

Arguments:
    -w, --workspace     The name of the workspace on the Seqera Platform.
    -id, --workflow_id   The unique identifiers for the workflow.
    -o, --output        The path to the output JSON file that will be created with workflow information.
    -s, --slack         Send Slack message with workflow metadata
    -d, --delete        Delete workflow after recording results. Will only delete successful workflows by default. If --force is true will delete all workflows.
    -f, --force         Force delete workflow even if it did not finish successfully

Example:
    python extract_metadata.py -w myworkspace -id 12345 -o workflow_details.json

Note: Ensure that the `TOWER_ACCESS_TOKEN` has been set in your environment before running the script.
"""

import argparse
import json
import logging
import os
import tarfile
import tempfile
import zipfile

from argparse import Namespace
from pathlib import Path
from seqerakit import seqeraplatform
from slack_sdk.web import WebClient
from typing import Any, Dict, List


# Slack table block maximum row limit (including header row)
SLACK_TABLE_MAX_ROWS = 100


def parse_args() -> Namespace:
    """
    Parse command-line arguments.

    Returns:
        Namespace: An argparse.Namespace object containing the arguments 'output',
        'workspace', and 'workflow_id'.
    """
    parser = argparse.ArgumentParser(
        description="Extract and process Seqera Platform workflow metadata."
    )
    parser.add_argument(
        "-l",
        "--log_level",
        default="INFO",
        choices=("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"),
        help="The desired log level (default: INFO).",
        type=str.upper,
    )
    parser.add_argument(
        "-o", "--output", type=str, required=True, help="Output filename for JSON file"
    )
    parser.add_argument(
        "-i",
        "--input",
        type=str,
        required=True,
        nargs="+",
        help="Seqera Platform workflow ID",
    )
    parser.add_argument(
        "-s",
        "--slack",
        action="store_true",
        help="Send Slack message with workflow metadata",
    )
    parser.add_argument(
        "-d",
        "--delete",
        action="store_true",
        help="Delete workflow after recording results. Will only delete successful workflows by default. If --force is true will delete all workflows.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force delete workflow even if it did not finish successfully",
    )
    parser.add_argument(
        "--slack_channel",
        type=str,
        help="Slack channel to send message to",
        default="C054QAK3FLZ",
    )
    return parser.parse_args()


def decompress_and_recompress_tar(
    tar_file: str, data: dict[str, Any], output_file: str
) -> str:
    """
    Decompresses the tar file, adds a Python dictionary as JSON, and recompresses the tar file.

    Args:
        tar_file (str): The path to the tar.gz file to decompress and recompress.
        data (dict): The Python dictionary to add as JSON.
        output_file (str): The name of the output file to write the recompressed tar.gz file to.

    Returns:
        str: The path to the recompressed tar.gz file.
    """
    # Decompress the tar file
    with tarfile.open(
        tar_file, "r:gz"
    ) as tar, tempfile.TemporaryDirectory() as tempdir:
        tempdir_path = Path(tempdir)
        tar.extractall(tempdir)

        # Add the Python dictionary as JSON
        data_json = Path(tempdir) / "workflow-info.json"
        with open(data_json, "w") as json_file:
            json_file.write(json.dumps(data))

        # Recompress the tar file
        with tarfile.open(output_file, "w:gz") as tar:
            for fn in tempdir_path.iterdir():
                p = tempdir_path.joinpath(fn)
                tar.add(p, arcname=p.name)

    return output_file


def get_runs_dump(
    seqera: seqeraplatform.SeqeraPlatform, workflow: dict[str, Any]
) -> str:
    """
    Run the `tw runs dump` command for a given workflow ID within a workspace and download archive as a tar.gz file.

    Args:
        seqera (SeqeraPlatform): An instance of the SeqeraPlatform class that interacts with the Seqera Platform CLI.
        workflow_id (str): The ID of the workflow to retrieve run data for.
        workspace (str): The name of the workspace in which the workflow was run.

    Returns:
        str: The name of the downloaded tar.gz file.
    """
    output_file = f"{workflow['workflowId']}.tar.gz"
    tmp_file = f"tmp.{output_file}"
    logging.debug(f"Using tmpfile: {tmp_file}")
    seqera.runs(
        "dump",
        "-id",
        workflow["workflowId"],
        "-o",
        tmp_file,
        "-w",
        str(workflow["workspaceId"]),
        json=True,
    )

    output_file = decompress_and_recompress_tar(tmp_file, workflow, output_file)
    os.remove(tmp_file)
    return output_file


def extract_workflow_data(tar_file: str) -> Dict[str, Any]:
    """
    Extract specified files from the tar archive generated by `tw runs dump` and load their contents as JSON.

    Args:
        tar_file (str): The path to the tar.gz file from `tw runs dump` to extract.
    Returns:
        dict: A dictionary where keys are the file names without extension and values are the text. If JSON it will be a dict, if any other it will be a string.
    """
    extracted_data = {}
    with tarfile.open(tar_file, "r:gz") as tar:
        for member in tar.getmembers():
            filename = Path(member.name).stem
            try:
                extracted_data[filename] = json.load(tar.extractfile(member))
            except json.JSONDecodeError:
                # Read in text as plain text for saving logs into list
                extracted_data[filename] = (
                    tar.extractfile(member).read().decode().split("\n")
                )

    return extracted_data


def create_failure_to_launch_workflow_data(workflow: dict[str, Any]) -> Dict[str, Any]:
    """
    Create a dictionary containing the workflow information for a workflow that failed to launch.

    Args:
        workflow (dict): The dictionary containing the workflow information.
    Returns:
        dict: A dictionary containing the workflow information.
    """
    return {
        "workflow": {
            "id": None,
            "projectName": workflow["workflowName"],
            "status": "FAILED_TO_LAUNCH",
            "errorMessage": workflow["error"].strip(),
        },
        "workflow-info": workflow,
        "workflow-launch": {"computeEnv": {"name": workflow["computeEnvironment"]}},
    }


def parse_json(
    json_data: dict[str, Any] | None, keys: Dict[str, str]
) -> Dict[str, Any]:
    """
    Parse a JSON object and return the values for the specified keys, including nested keys.

    Args:
        json_data (dict): The JSON input data to parse.
        keys_list (dict): A key value pair to extract from nested JSON. The key will be used for
            assignment in the output dictionary and the value will be what is extracted from the
            input JSON. Nested keys should be denoted with a period.

    Returns:
        dict: A dictionary of extracted key-value pairs from the JSON data.
    """
    update_dict = {}
    for key, val in keys.items():
        try:
            value = json_data
            for part in val.split("."):
                value = value.get(part)
                update_dict[key] = value
        except (KeyError, TypeError, AttributeError):
            update_dict[key] = None
    return update_dict


def delete_run_on_platform(
    seqera: seqeraplatform.SeqeraPlatform,
    run_info: Dict[str, Any],
    force: bool = False,
) -> Dict[str, str | bool]:
    """
    Delete a workflow run from the Seqera Platform.

    Args:
        seqera (SeqeraPlatform): An instance of the SeqeraPlatform class that interacts with the Seqera Platform CLI.
        run_info (dict): The dictionary containing the workflow run information.
        workspace (str): The name of the workspace in which the workflow was run.
        force (bool): Force delete workflow even if it did not finish successfully

    Returns:
        dict: A dictionary containing the workflow ID and a boolean indicating whether the run was deleted.
    """
    # Create default output:
    default_output = {
        "id": run_info["workflow"]["id"],
        "workspaceRef": run_info["workflow-info"]["workspaceRef"],
        "deleted": False,
    }

    # Skip deletion if workflow failed to launch (no workflow ID)
    if run_info["workflow"]["id"] is None:
        logging.info(
            f"Skipping deletion for failed launch: {run_info['workflow-info']['workflowName']}"
        )
        return default_output

    # Check if run finish and delete if true
    if run_info["workflow"]["status"] == "SUCCEEDED" or force:
        try:
            logging.info(f"Deleting run {run_info['workflow']['id']}")

            # Get workspaceId from workflow-metadata (successful launches) or workflow-info (failed launches)
            workspace_id = run_info.get("workflow-metadata", {}).get(
                "workspaceId"
            ) or run_info["workflow-info"].get("workspaceId")

            args = [
                "delete",
                "-id",
                run_info["workflow"]["id"],
                "-w",
                str(workspace_id),
            ]

            if force:
                args.extend(["--force"])

            delete_dict = seqera.runs(
                *args,
                to_json=True,
            )
            delete_dict.update({"deleted": True})

            return delete_dict
        except json.JSONDecodeError as err:
            logging.error(f"Error deleting run {run_info['workflow']['id']}: {err}")
        return default_output
    else:
        return default_output


def get_status_emoji(status: str) -> str:
    """
    Get an emoji representation for a workflow status.

    Args:
        status (str): The workflow status.

    Returns:
        str: An emoji representing the status.
    """
    status_map = {
        "SUCCEEDED": "✅",
        "FAILED": "❌",
        "FAILED_TO_LAUNCH": "🚫",
        "RUNNING": "🚀",
        "SUBMITTED": "⏳",
        "CANCELLED": "⏸️",
        "UNKNOWN": "❓",
    }
    return status_map.get(status, "❓")


def create_table_cell_raw(text: str) -> Dict[str, str]:
    """
    Create a raw text table cell.

    Args:
        text (str): The text content.

    Returns:
        dict: A raw_text table cell.
    """
    return {"type": "raw_text", "text": str(text) if text else "-"}


def create_table_cell_link(text: str, url: str) -> Dict[str, Any]:
    """
    Create a rich text table cell with a hyperlink.

    Args:
        text (str): The link text to display.
        url (str): The URL to link to.

    Returns:
        dict: A rich_text table cell with a link.
    """
    if not url or url == "-":
        return create_table_cell_raw(text)

    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "link", "text": text, "url": url}],
            }
        ],
    }


def build_workflow_summary(parsed_data: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Build a summary of workflow statuses.

    Args:
        parsed_data (list): List of parsed workflow data.

    Returns:
        dict: Summary statistics with status counts.
    """
    summary = {"total": len(parsed_data), "succeeded": 0, "failed": 0, "other": 0}

    for workflow in parsed_data:
        status = workflow.get("status", "UNKNOWN")
        if status == "SUCCEEDED":
            summary["succeeded"] += 1
        elif status in ("FAILED", "FAILED_TO_LAUNCH"):
            summary["failed"] += 1
        else:
            summary["other"] += 1

    return summary


def sort_workflows(workflows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort workflows by: status (failed first), pipeline name, compute env, workspace.

    Args:
        workflows: List of workflow dictionaries.

    Returns:
        Sorted list of workflows.
    """
    # Define status priority (lower number = higher priority = appears first)
    status_priority = {
        "FAILED": 1,
        "UNKNOWN": 2,
        "CANCELLED": 3,
        "RUNNING": 4,
        "SUBMITTED": 5,
        "SUCCEEDED": 6,
    }

    def sort_key(workflow: Dict[str, Any]) -> tuple:
        status = workflow.get("status", "UNKNOWN")
        status = status.upper() if status else "UNKNOWN"

        pipeline = workflow.get("pipeline", "") or ""
        compute = workflow.get("computeEnv", "") or ""
        workspace = workflow.get("workspace", "") or ""

        # Get priority (default to 2 for unknown statuses)
        priority = status_priority.get(status, 2)

        return (priority, pipeline.lower(), compute.lower(), workspace.lower())

    return sorted(workflows, key=sort_key)


def build_table_block(parsed_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build a Slack table block to display workflows sorted by status, pipeline, compute env, and workspace.

    Args:
        parsed_data (list): List of parsed workflow data (max 100 rows).

    Returns:
        dict: A Slack table block.
    """
    # Sort workflows before building table
    sorted_data = sort_workflows(parsed_data)

    # Build table rows
    rows = []

    # Header row - match original column order: pipeline, workspace, computeEnv, status, workflowUrl
    rows.append(
        [
            create_table_cell_raw("Pipeline"),
            create_table_cell_raw("Workspace"),
            create_table_cell_raw("Compute Environment"),
            create_table_cell_raw("Status"),
            create_table_cell_raw("Link"),
        ]
    )

    # Data rows - sorted by status (failed first), then pipeline, then compute env, then workspace
    for workflow in sorted_data:
        status = workflow.get("status", "UNKNOWN")
        emoji = get_status_emoji(status)
        status_text = f"{emoji} {status}"
        pipeline = workflow.get("pipeline", "Unknown")
        workspace = workflow.get("workspace", "-")
        compute = workflow.get("computeEnv", "-")
        workflow_url = workflow.get("workflowUrl", "")

        rows.append(
            [
                create_table_cell_raw(pipeline),
                create_table_cell_raw(workspace),
                create_table_cell_raw(compute),
                create_table_cell_raw(status_text),
                (
                    create_table_cell_link("View Run", workflow_url)
                    if workflow_url and workflow_url != "-"
                    else create_table_cell_raw("-")
                ),
            ]
        )

    # Create table block with column settings
    table_block = {
        "type": "table",
        "column_settings": [
            {"align": "left", "is_wrapped": True},  # Pipeline (allow wrapping)
            {"align": "left"},  # Workspace
            {"align": "left"},  # Compute Environment
            {"align": "left"},  # Status
            {"align": "center"},  # Link
        ],
        "rows": rows,
    }

    return table_block


def split_workflows_for_messages(
    parsed_data: List[Dict[str, Any]], max_rows_per_table: int = SLACK_TABLE_MAX_ROWS
) -> List[List[Dict[str, Any]]]:
    """
    Split workflows into batches for Slack table blocks.
    Slack table blocks support maximum 100 rows (including header).

    Args:
        parsed_data (list): List of all workflow data.
        max_rows_per_table (int): Maximum data rows per table (default SLACK_TABLE_MAX_ROWS).

    Returns:
        list: List of workflow batches, each with max SLACK_TABLE_MAX_ROWS workflows.
    """
    if not parsed_data:
        return []

    # Split into batches of max_rows_per_table
    batches = []
    for i in range(0, len(parsed_data), max_rows_per_table):
        batch = parsed_data[i : i + max_rows_per_table]
        batches.append(batch)

    return batches


def send_slack_message(
    extracted_data: List[Dict["str", Any]],
    data_to_send: Dict[str, str],
    filepath: Path,
    slack_channel: str,
) -> None:
    """
    Send Slack message(s) with workflow metadata using table blocks and upload JSON file as threaded reply.
    Will send multiple messages if more than SLACK_TABLE_MAX_ROWS workflows (table block limit).

    Args:
        extracted_data (list): The list of dictionaries containing the workflow metadata.
        data_to_send (dict): The dictionary the name of each table element (as keys) with each field within the dictionary to send as a value.
        filepath (Path): The path to the JSON file to attach to the Slack message. Can be zipped up for convenience.
        slack_channel (str): The Slack channel to send the message to.
    Returns:
        None
    """
    parsed_data = [parse_json(x, data_to_send) for x in extracted_data]

    # Calculate summary statistics and determine attachment color
    summary = build_workflow_summary(parsed_data)
    if summary["failed"] > 0:
        color = "#FF0000"  # Red for failures
    elif summary["succeeded"] == summary["total"]:
        color = "#36A64F"  # Green for all success
    else:
        color = "#FFB84D"  # Orange for mixed results

    # Initialize Slack client
    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    auth_result = slack_client.auth_test()
    if not auth_result.get("ok", False):
        raise Exception("Invalid Slack token")

    # Split workflows into batches of SLACK_TABLE_MAX_ROWS
    workflow_batches = split_workflows_for_messages(
        parsed_data, max_rows_per_table=SLACK_TABLE_MAX_ROWS
    )
    num_messages = len(workflow_batches)

    logging.info(
        f"Sending {num_messages} Slack message(s) with {len(parsed_data)} total workflows"
    )

    # Create fallback text with summary statistics for notifications
    fallback_text = f"Workflow Report ({summary['total']} workflows: {summary['succeeded']} ✅, {summary['failed']} ❌)"

    # Send each batch as a separate message
    for idx, batch in enumerate(workflow_batches):
        message_num = idx + 1

        # Build blocks for message
        blocks = []

        # Add part indicator if multiple messages
        if num_messages > 1:
            part_block = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"_Part {message_num} of {num_messages}_ (Showing {len(batch)} workflows)",
                },
            }
            blocks.append(part_block)

        # Build table block for this batch
        table_block = build_table_block(batch)

        # Post message with table first, then upload file as threaded reply
        if message_num == 1:
            # Step 1: Post the main message with summary and table
            # Table blocks MUST be in attachments, not in main blocks array
            message_response = slack_client.chat_postMessage(
                channel=slack_channel,
                blocks=blocks,
                attachments=[{"color": color, "blocks": [table_block]}],
                text=fallback_text,
            )
            if not message_response.get("ok", False):
                raise Exception(f"Error posting Slack message: {message_response}")

            # Step 2: Upload file as a threaded reply to the main message
            # Get the message timestamp (ts) to use as thread_ts
            parent_ts = message_response["ts"]
            file_response = slack_client.files_upload_v2(
                title=filepath.stem,
                file=filepath.as_posix(),
                channel=slack_channel,
                thread_ts=parent_ts,  # Attach file as reply in thread
            )
            if not file_response.get("ok", False):
                raise Exception(f"Error with Slack file upload: {file_response}")
        else:
            # Subsequent messages (for >SLACK_TABLE_MAX_ROWS workflows) - just post with table
            # Include summary stats in fallback text for notifications
            response = slack_client.chat_postMessage(
                channel=slack_channel,
                blocks=blocks,
                attachments=[{"color": color, "blocks": [table_block]}],
                text=f"{fallback_text} - Part {message_num} of {num_messages}",
            )
            if not response.get("ok", False):
                raise Exception(f"Error posting Slack message: {response}")


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=args.log_level)

    seqera = seqeraplatform.SeqeraPlatform()

    logging.info("Reading workflow details from JSON file...")
    workflow_details = []
    for launchJson in args.input:
        with open(launchJson, "r") as infile:
            # Be aware this is expecting a list of workflows in the JSON file
            workflow_details.append(json.load(infile))

    logging.info("Getting workflow run data...")
    # Flattens list of lists containing dicts
    tar_files = [
        get_runs_dump(seqera, workflow)
        for workflowList in workflow_details
        for workflow in workflowList
        if workflow["launchSuccess"]
    ]

    logging.info("Extracting workflow metadata...")
    extracted_data = [extract_workflow_data(tar_file) for tar_file in tar_files]

    # Add failed runs for reporting
    # Create fake JSON dump
    failed_runs = [
        create_failure_to_launch_workflow_data(workflow)
        for workflowList in workflow_details
        for workflow in workflowList
        if not workflow["launchSuccess"]
    ]
    extracted_data.extend(failed_runs)

    logging.info("Writing workflow metadata to JSON file...")
    with open(args.output, "w") as outfile:
        json.dump(extracted_data, outfile, indent=4)

    logging.info("Zipping workflow metadata JSON file...")

    zipfile_out = Path(args.output).with_suffix(".zip")
    with zipfile.ZipFile(zipfile_out, "w", zipfile.ZIP_DEFLATED) as outzip:
        outzip.write(args.output)

    logging.info(f"Workflow metadata written to {args.output}.")

    if args.slack:
        # Get critical info, flatten and rename to user friendly values
        data_to_extract = {
            "pipeline": "workflow.projectName",
            "workspace": "workflow-info.workspaceRef",
            "computeEnv": "workflow-launch.computeEnv.name",
            "status": "workflow.status",
            "workflowUrl": "workflow-metadata.runUrl",
        }
        send_slack_message(
            extracted_data,
            data_to_extract,
            zipfile_out,
            slack_channel=args.slack_channel,
        )

    # On success, delete if pipeline succeeded
    if args.delete:
        for run in extracted_data:
            delete_run_on_platform(seqera, run, force=args.force)


if __name__ == "__main__":
    main()
