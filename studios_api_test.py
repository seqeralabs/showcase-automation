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
    parentCheckpoint: str | None = None
    user: dict
    name: str
    statusInfo: StudioStatus
    description: str | None = None
    studioUrl: str
    computeEnv: dict


class StudioList(pydantic.BaseModel):
    """A list of Data Studios."""

    studios: list[Studio]
    totalSize: int


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
    return parser.parse_args()


# Set up API request headers
def get_headers() -> dict:
    access_token = os.getenv("TOWER_ACCESS_TOKEN")
    if not access_token:
        raise EnvironmentError("TOWER_ACCESS_TOKEN environment variable not set.")
    return {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}


# Query the Seqera API
def seqera_api_get(
    base_url: str, workspace_id: int, endpoint: str, params=None
) -> StudioList:
    url = f"{base_url}/{endpoint}"

    if params is None:
        params = {}
    params.setdefault("workspaceId", workspace_id)  # Ensure workspaceId is included

    headers = get_headers()
    response = requests.get(url, headers=headers, params=params)

    response.raise_for_status()  # Raise an exception for HTTP error codes
    studio_list = StudioList.model_validate(
        response.json()
    )  # Turn response into object
    return studio_list


def print_studios_table(studios_list: StudioList) -> None:
    """Print studios data as a formatted table"""

    # Extract relevant fields for each studio
    # Merge studio and statusInfo into a single dictionary
    table_data = [
        studio.dict(include={"name", "sessionId"}) | studio.statusInfo.dict()
        for studio in studios_list.studios
    ]

    # Print as table
    print(tabulate(table_data, headers="keys", tablefmt="plain", missingval="-"))


# Main function to orchestrate the process
def main():
    args = parse_args()

    # Query the Seqera API for studios
    studios = seqera_api_get(args.base_url, args.workspace_id, "studios")

    # Print the response data
    print_studios_table(studios)


if __name__ == "__main__":
    main()
