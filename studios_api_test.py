import argparse
import os
import pydantic
import requests
from tabulate import tabulate


class StudioStatus(pydantic.BaseModel):
    """Status of the Studio"""

    status: str | None
    message: str | None
    lastUpdate: str | None


class Studio(pydantic.BaseModel):
    """Studio class."""

    sessionId: str
    workspaceId: int
    workspaceName: str | None = None
    user: dict
    name: str
    statusInfo: StudioStatus
    description: str | None = None
    studioUrl: str
    computeEnv: dict

    def model_post_init(self, __context) -> None:
        # Get the workspace name from the workspace ID
        self.workspaceName = get_workspace_name(self.workspaceId)


class StudioList(pydantic.BaseModel):
    """A list of Studios."""

    studios: list[Studio]
    totalSize: int

    def __add__(self, other: "StudioList") -> "StudioList":
        return StudioList(
            studios=self.studios + other.studios,
            totalSize=self.totalSize + other.totalSize,
        )


def create_table_cell_raw(text: str) -> dict[str, str]:
    """
    Create a raw text table cell.

    Args:
        text (str): The text content.

    Returns:
        dict: A raw_text table cell.
    """
    return {"type": "raw_text", "text": str(text) if text else "-"}


def create_table_cell_link(text: str, url: str) -> dict[str, str | list]:
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


def get_studio_status_emoji(status: str) -> str:
    """
    Get an emoji representation for a studio status.

    Args:
        status (str): The studio status.

    Returns:
        str: An emoji representing the status.
    """
    status = (status or "").lower()
    status_map = {
        "running": "✅",
        "started": "🚀",
        "stopped": "⏸️",
        "failed": "❌",
        "errored": "❌",
        "unknown": "❓",
    }
    return status_map.get(status, "❓")


def sort_studios(studios: list[Studio]) -> list[Studio]:
    """
    Sort studios by: status (failed/errored first), then studio name, then workspace.

    Args:
        studios: List of Studio objects.

    Returns:
        Sorted list of studios.
    """
    # Define status priority (lower number = higher priority = appears first)
    status_priority = {
        "failed": 1,
        "errored": 2,
        "stopped": 3,
        "unknown": 4,
        "started": 5,
        "running": 6,
    }

    def sort_key(studio: Studio) -> tuple:
        status = (studio.statusInfo.status or "unknown").lower()
        studio_name = studio.name or ""
        workspace_name = studio.workspaceName or ""

        # Get priority (default to 4 for unknown statuses)
        priority = status_priority.get(status, 4)

        return (priority, studio_name.lower(), workspace_name.lower())

    return sorted(studios, key=sort_key)


def build_studio_summary(studios: list[Studio]) -> dict[str, int]:
    """
    Build a summary of studio statuses.

    Args:
        studios (list): List of Studio objects.

    Returns:
        dict: Summary statistics with status counts.
    """
    summary = {"total": len(studios), "running": 0, "failed": 0, "other": 0}

    for studio in studios:
        status = (studio.statusInfo.status or "unknown").lower()
        if status in ("running", "started"):
            summary["running"] += 1
        elif status in ("failed", "errored"):
            summary["failed"] += 1
        else:
            summary["other"] += 1

    return summary


# Parse command-line arguments
def parse_args() -> argparse.Namespace:
    VALID_STATUSES = ["stopped", "started", "running", "failed", "errored"]

    parser = argparse.ArgumentParser(
        description="Query Seqera Platform API to get information about studios."
    )
    parser.add_argument(
        "-url",
        "--base_url",
        type=str,
        help="Base URL of the Seqera Platform instance",
        required=False,
        default=os.environ.get("TOWER_API_ENDPOINT"),
    )
    parser.add_argument(
        "-w",
        "--workspace_id",
        type=str,
        nargs="+",
        help="Workspace ID that contains resources",
        required=True,
    )
    parser.add_argument("--raw", action="store_true", help="Print raw JSON response")
    parser.add_argument(
        "-s",
        "--status",
        type=str,
        choices=VALID_STATUSES,
        help="Filter studios by status (stopped, started, running, errored)",
        required=False,
    )
    parser.add_argument(
        "-c",
        "--slack_channel",
        type=str,
        help="Slack channel to send the message to",
        required=False,
        default=os.environ.get("SLACK_CHANNEL"),
    )
    parser.add_argument(
        "--slack",
        action="store_true",
        help="Send a Slack message with the studios table",
        required=False,
    )
    return parser.parse_args()


# Set up API request headers
def get_headers() -> dict:
    access_token = os.getenv("TOWER_ACCESS_TOKEN")
    if not access_token:
        raise EnvironmentError("TOWER_ACCESS_TOKEN environment variable not set.")
    return {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}


# Query the Seqera API
# Studios endpoint = "studios"
# User-info endpoint = "user-info"
# List workspaces endpoint = "user/${userId}/workspaces"
def seqera_api_get(
    endpoint: str,
    params=None,
    base_url: str | None = None,
) -> dict:
    base_url = (
        base_url or os.getenv("TOWER_API_ENDPOINT") or "https://cloud.seqera.io/api"
    )
    url = f"{base_url}/{endpoint}"

    if params is None:
        params = {}

    headers = get_headers()
    response = requests.get(url, headers=headers, params=params)

    # Provide better error messages for common HTTP errors
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if response.status_code == 403:
            raise PermissionError(
                f"Access denied to {endpoint}. "
                f"Check that your TOWER_ACCESS_TOKEN has permissions for workspace(s): {params.get('workspaceId', 'N/A')}"
            ) from e
        elif response.status_code == 404:
            raise ValueError(
                f"Endpoint not found: {endpoint}. "
                f"Check that the workspace ID is correct: {params.get('workspaceId', 'N/A')}"
            ) from e
        else:
            raise

    # Check if response has content before trying to parse JSON
    if not response.content:
        raise ValueError(f"Empty response from API endpoint: {endpoint}")

    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as e:
        raise ValueError(
            f"Invalid JSON response from {endpoint}. "
            f"Status: {response.status_code}, "
            f"Content: {response.text[:200]}"
        ) from e


def get_workspace_name(workspace_id: int) -> str:
    user_info = seqera_api_get(endpoint="user-info")
    user_id = user_info["user"]["id"]
    workspace_details = seqera_api_get(endpoint=f"user/{user_id}/workspaces")
    workspace = [
        f"{w['orgName']}/{w['workspaceName']}"
        for w in workspace_details["orgsAndWorkspaces"]
        if w["workspaceId"] == int(workspace_id)
    ]
    return workspace[0]


def get_studios(base_url: str, workspace_ids: list[int]) -> StudioList:
    studios = StudioList(studios=[], totalSize=0)
    for workspace_id in workspace_ids:
        studios_responses = seqera_api_get(
            endpoint="studios", params={"workspaceId": workspace_id}
        )
        studios += StudioList.model_validate(studios_responses)
    return studios


def build_studios_table_block(studios_list: StudioList) -> dict:
    """
    Build a Slack table block to display studios sorted by status, studio name, and workspace.

    Args:
        studios_list (StudioList): StudioList object containing studios.

    Returns:
        dict: A Slack table block.
    """
    # Sort studios before building table
    sorted_studios = sort_studios(studios_list.studios)

    # Build table rows
    rows = []

    # Header row: Workspace, Studio Name, Status, Message, Last Update, Link
    rows.append(
        [
            create_table_cell_raw("Workspace"),
            create_table_cell_raw("Studio Name"),
            create_table_cell_raw("Status"),
            create_table_cell_raw("Message"),
            create_table_cell_raw("Last Update"),
            create_table_cell_raw("Link"),
        ]
    )

    # Data rows - sorted by status (failed first), then studio name, then workspace
    for studio in sorted_studios:
        status = studio.statusInfo.status or "unknown"
        emoji = get_studio_status_emoji(status)
        status_text = f"{emoji} {status}"
        workspace = studio.workspaceName or "-"
        studio_name = studio.name or "-"
        message = studio.statusInfo.message or "-"
        last_update = studio.statusInfo.lastUpdate or "-"
        studio_url = studio.studioUrl or ""

        rows.append(
            [
                create_table_cell_raw(workspace),
                create_table_cell_raw(studio_name),
                create_table_cell_raw(status_text),
                create_table_cell_raw(message),
                create_table_cell_raw(last_update),
                (
                    create_table_cell_link("View Studio", studio_url)
                    if studio_url
                    else create_table_cell_raw("-")
                ),
            ]
        )

    # Create table block with column settings
    table_block = {
        "type": "table",
        "column_settings": [
            {"align": "left"},  # Workspace
            {"align": "left", "is_wrapped": True},  # Studio Name (allow wrapping)
            {"align": "left"},  # Status
            {"align": "left", "is_wrapped": True},  # Message (allow wrapping)
            {"align": "left"},  # Last Update
            {"align": "center"},  # Link
        ],
        "rows": rows,
    }

    return table_block


def send_slack_message(studios_list: StudioList, slack_channel: str) -> None:
    """
    Send a Slack message with the studios table using table blocks.

    Args:
        studios_list (StudioList): StudioList object containing studios.
        slack_channel (str): The Slack channel to send the message to.

    Returns:
        None
    """
    from slack_sdk import WebClient

    # Calculate summary statistics and determine attachment color
    summary = build_studio_summary(studios_list.studios)
    if summary["failed"] > 0:
        color = "#FF0000"  # Red for failures
    elif summary["running"] == summary["total"]:
        color = "#36A64F"  # Green for all running
    else:
        color = "#FFB84D"  # Orange for mixed results

    # Initialize Slack client
    webclient = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    _auth_test = webclient.auth_test()
    if not _auth_test.data.get("ok", False):
        raise Exception("Invalid Slack token")

    # Build table block
    table_block = build_studios_table_block(studios_list)

    # Create fallback text with summary statistics for notifications
    fallback_text = f"Studio Status Report ({summary['total']} studios: {summary['running']} running ✅, {summary['failed']} failed ❌)"

    # Post message with table block in attachments
    response = webclient.chat_postMessage(
        channel=slack_channel,
        attachments=[{"color": color, "blocks": [table_block]}],
        text=fallback_text,
    )

    if not response.data.get("ok", False):
        raise Exception(f"Error sending Slack message: {response}")


# Main function to orchestrate the process
def main():
    args = parse_args()

    studios = get_studios(args.base_url, args.workspace_id)

    # Print summary to console
    summary = build_studio_summary(studios.studios)
    print(
        f"Studio Status Report: {summary['total']} studios ({summary['running']} running, {summary['failed']} failed, {summary['other']} other)"
    )

    if args.slack:
        send_slack_message(studios, args.slack_channel)


if __name__ == "__main__":
    main()
