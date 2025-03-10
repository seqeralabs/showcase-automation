import argparse
import os
import pydantic
import requests
from tabulate import tabulate


class StudioStatus(pydantic.BaseModel):
    """Status of the Data Studio"""

    status: str | None
    message: str | None
    lastUpdate: str | None


class Studio(pydantic.BaseModel):
    """Data Studio class."""

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
    """A list of Data Studios."""

    studios: list[Studio]
    totalSize: int

    def __add__(self, other: "StudioList") -> "StudioList":
        return StudioList(
            studios=self.studios + other.studios,
            totalSize=self.totalSize + other.totalSize,
        )


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
# DataStudios endpoint = "studios"
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

    response.raise_for_status()  # Raise an exception for HTTP error codes
    return response.json()


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


def studios_table(studios_list: StudioList) -> str:
    """Print studios data as a formatted table"""

    # Extract relevant fields for each studio
    # Merge studio and statusInfo into a single dictionary
    table_data = [
        studio.model_dump(include={"workspaceName", "name"})
        | studio.statusInfo.model_dump()
        for studio in studios_list.studios
    ]

    # Print as table
    return tabulate(table_data, headers="keys", tablefmt="plain", missingval="-")


def send_slack_message(table: str, slack_channel: str) -> None:
    """Send a Slack message with the studios table"""
    from slack_sdk import WebClient

    webclient = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    _auth_test = webclient.auth_test()
    if not _auth_test.data.get("ok", False):
        raise Exception("Invalid Slack token")

    response = webclient.chat_postMessage(
        channel=slack_channel, text="```" + table + "```"
    )

    if not response.data.get("ok", False):
        raise Exception("Error sending Slack message")


# Main function to orchestrate the process
def main():
    args = parse_args()

    studios = get_studios(args.base_url, args.workspace_id)

    # Print the response data
    table = studios_table(studios)
    print(table)
    if args.slack:
        send_slack_message(table, args.slack_channel)


if __name__ == "__main__":
    main()
