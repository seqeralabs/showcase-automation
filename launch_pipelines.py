import argparse
import datetime
import json
import logging
import pydantic
import uuid

from pathlib import Path
from seqerakit import seqeraplatform

# Globals
# Global UUID for the launch name
workflow_uuid = uuid.uuid4()
# Global date for the launch name
date = datetime.datetime.now().strftime("%Y-%m-%d")


class SeqeraKitError(Exception):
    """Base class for exceptions in this module."""

    pass


class Pipeline(pydantic.BaseModel):
    name: str
    url: str
    latest: bool
    profiles: list[str]


class ComputeEnvironment(pydantic.BaseModel):
    name: str
    ref: str
    workdir: str
    workspace_id: str


class LaunchConfig(pydantic.BaseModel):
    pipeline: "Pipeline"
    compute_environment: "ComputeEnvironment"


# Need to use update_forward_refs() to resolve circular references.
LaunchConfig.update_forward_refs()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a matrix of pipelines and compute environments."
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
        "-o", "--output", type=str, required=True, help="Output filename for JSON file."
    )
    parser.add_argument(
        "-c",
        "--compute-envs",
        type=str,
        required=True,
        help="Path to JSON file of compute environments.",
    )
    parser.add_argument(
        "-p",
        "--pipelines",
        type=str,
        required=True,
        help="Path to JSON file of pipelines.",
    )
    parser.add_argument(
        "-i",
        "--include",
        type=str,
        required=False,
        nargs="+",
        help="List of additional pipeline and compute environment combinations to include.",
    )
    parser.add_argument(
        "-e",
        "--exclude",
        type=str,
        required=False,
        nargs="+",
        help="List of pipeline and compute environment combinations to exclude.",
    )
    return parser.parse_args()


def read_pipeline_json(path: str) -> list[Pipeline]:
    logging.info("Reading pipeline details...")
    with open(path) as pipeline_file:
        pipelines = json.load(pipeline_file)
    return [Pipeline(**pipeline) for pipeline in pipelines]


def read_compute_env_json(path: str) -> list[ComputeEnvironment]:
    logging.info("Reading compute environment details...")
    with open(path) as compute_env_file:
        compute_envs = json.load(compute_env_file)
    return [ComputeEnvironment(**compute_env) for compute_env in compute_envs]


def read_include_json(path: str) -> list[LaunchConfig]:
    logging.info("Reading include details...")
    # check me
    with open(path) as include_file:
        include = json.load(include_file)
    return [LaunchConfig(**launch_config) for launch_config in include]


def create_launch_config(
    pipelines: list[Pipeline],
    compute_envs: list[ComputeEnvironment],
    include: list[LaunchConfig] = [],
) -> list[LaunchConfig]:
    launch_configs = []
    # Might be able to do this cleaner with itertools.combinations()
    for pipeline in pipelines:
        for compute_env in compute_envs:
            launch_config = LaunchConfig(
                pipeline=pipeline, compute_environment=compute_env
            )
            launch_configs.append(launch_config)
    for launch_config in include:
        launch_configs.append(launch_config)
    return launch_configs


def filter_launch_configs(
    launch_configs: list[LaunchConfig],
    include: list[LaunchConfig],
    exclude: list[LaunchConfig],
) -> list[LaunchConfig]:
    filtered_launch_configs = []
    for launch_config in launch_configs:
        if include:
            if launch_config in include:
                filtered_launch_configs.append(launch_config)
        elif exclude:
            if launch_config not in exclude:
                filtered_launch_configs.append(launch_config)
        else:
            filtered_launch_configs.append(launch_config)
    return filtered_launch_configs


def launch_pipeline(
    seqera: seqeraplatform.SeqeraPlatform,
    launch_config: LaunchConfig,
    wait: str = "SUBMITTED",
) -> dict[str, str]:
    # Pre-create some variables to make things easier.
    run_name = "_".join(
        [
            launch_config.pipeline.name,
            launch_config.compute_environment.name,
            date,
            str(workflow_uuid),
        ]
    )
    profiles = ",".join(launch_config.pipeline.profiles)
    # It's never good to create a path with string handling but it's the quickest way here.
    workdir = "/".join([launch_config.compute_environment.workdir, run_name])

    # Launch the pipeline and wait for submission.
    logging.info(
        f"Launching pipeline {launch_config.pipeline.name} on {launch_config.compute_environment.name}."
    )
    try:
        launched_pipeline = seqera.launch(
            "--workspace",
            launch_config.compute_environment.workspace_id,
            "--compute-env",
            launch_config.compute_environment.ref,
            "--work-dir",
            launch_config.compute_environment.workdir,
            "--name",
            run_name,
            "--profile",
            profiles,
            "--wait",
            wait,
            launch_config.pipeline.url,
            to_json=True,
        )
    except json.decoder.JSONDecodeError as err:
        logging.error(f"Failed to launch pipeline {run_name}.")
        logging.debug(err.doc)
        # Raise pipeline launch error here:
        raise SeqeraKitError(err.doc)
    return launched_pipeline


def launch_pipelines(
    seqera: seqeraplatform.SeqeraPlatform, launch_configs: list[LaunchConfig]
) -> list[dict[str, str]]:
    logging.info("Launching pipelines.")
    launched_pipelines = [
        launch_pipeline(seqera, launch_config) for launch_config in launch_configs
    ]
    logging.info("Pipelines launched.")
    return launched_pipelines


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO)

    seqera = seqeraplatform.SeqeraPlatform()

    pipelines = read_pipeline_json(args.pipelines)
    compute_envs = read_compute_env_json(args.compute_envs)
    launch_configs = create_launch_config(pipelines, compute_envs)
    # include = read_include_json(args.include)
    # exclude = read_exclude_json(args.exclude)
    # filtered_launch_configs = filter_launch_configs(launch_configs, include, exclude)
    launched_pipelines = launch_pipelines(seqera, launch_configs)

    with open(args.output, "w") as output_file:
        json.dump(launched_pipelines, output_file, indent=4)


if __name__ == "__main__":
    main()
