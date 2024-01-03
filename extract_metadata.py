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
from slack_sdk.webhook import WebhookClient
from tabulate import tabulate
from typing import Any, Dict, List



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
        default="C054QAK3FLZ"
    )
    return parser.parse_args()

def decompress_and_recompress_tar(tar_file: str, data: dict[str, Any], output_file: str) -> str:
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
    with tarfile.open(tar_file, "r:gz") as tar, tempfile.TemporaryDirectory() as tempdir:
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
    output_file = f"{workflow["workflowId"]}.tar.gz"
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
        "workflow-launch": {
            "computeEnv": {
                "name": workflow["computeEnvironment"]
            }
        },
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
) -> Dict[str, str | bool] | None:
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

    # Check if run finish and delete if true
    if run_info["workflow"]["status"] == "SUCCEEDED" or force:
        try:
            logging.info(f"Deleting run {run_info['workflow']['id']}")

            args = [
                "delete",
                "-id",
                run_info["workflow"]["id"],
                "-w",
                str(run_info["workflow-metadata"]["workspaceId"]),
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
    else:
        return default_output


def send_slack_message(
    extracted_data: List[Dict["str", Any]], data_to_send: Dict[str, str], filepath: Path, slack_channel: str
) -> None:
    """
    Send a Slack message with the workflow metadata as a formatted table.

    Args:
        extracted_data (list): The list of dictionaries containing the workflow metadata.
        data_to_send (dict): The dictionary the name of each table element (as keys) with each field within the dictionary to send as a value.
        filepath (str): The path to the JSON file to attach to the Slack message. Can be zipped up for convenience.
        slack_channel (str): The Slack channel to send the message to.
    Returns:
        None
    """
    parsed_data = [parse_json(x, data_to_send) for x in extracted_data]

    table = tabulate(parsed_data, headers="keys", tablefmt="simple", missingval="-")

    # Send Slack Message
    # webhookclient = WebhookClient(os.environ["SLACK_HOOK_URL"])
    # response = webhookclient.send(
    #     text="```" + table + "```", headers={"Content-type": "application/json"}
    # )

    # We can possibly attach the JSON as a file but not supported by API
    # We might be able to use file.upload API: https://api.slack.com/tutorials/uploading-files-with-python
    webclient = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    _auth_test = webclient.auth_test()
    if not _auth_test.data.get("ok", False):
        raise Exception("Invalid Slack token")

    file_upload = webclient.files_upload_v2(
        title=filepath.stem,
        file=filepath.as_posix(),
        initial_comment="```" + table + "```",
        channel=slack_channel,
    )

    if file_upload.status_code != 200:
        raise Exception("Error with Slack file upload")


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
            "platform": "service-info.version",
            "nextflow": "workflow.nextflow.version",
            "revision": "workflow-launch.revision",
            "workflowUrl": "workflow-metadata.runUrl",
            "error": "workflow.errorMessage"
        }
        send_slack_message(extracted_data, data_to_extract, zipfile_out, slack_channel=args.slack_channel)

    # On success, delete if pipeline succeeded
    if args.delete:
        for run in extracted_data:
            delete_run_on_platform(seqera, run, force=args.force)


if __name__ == "__main__":
    main()

