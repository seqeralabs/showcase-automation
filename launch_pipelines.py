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
workflow_uuid = str(uuid.uuid4()).replace("-", "")[:15]
# Global date for the launch name
date = datetime.datetime.now().strftime("%Y%m%d")


class SeqeraKitError(Exception):
    """Exception for failure to use Tower CLI."""

    pass


class Pipeline(pydantic.BaseModel):
    """A pipeline to launch."""

    name: str
    url: str
    latest: bool
    profiles: list[str]


class ComputeEnvironment(pydantic.BaseModel):
    """A compute environment to launch a pipeline on."""

    name: str
    ref: str
    workdir: str
    workspace_id: str


class LaunchConfig(pydantic.BaseModel):
    """A pipeline and compute environment to launch a pipeline on

    TODO: Make this a full-fledged class with methods.
    """

    pipeline: "Pipeline"
    compute_environment: "ComputeEnvironment"

    def __eq__(self, other, strict: bool = False) -> bool:
        # Check classes make sense
        if other.__class__ is self.__class__:
            # If strict, check the entire class matches
            if strict:
                return self == other
            # Checks pipeline name and compute env name match
            # Will ignore if other variables are different.
            else:
                return self.pipeline.dict().get("name") == other.pipeline.dict().get(
                    "name"
                ) and self.compute_environment.dict().get(
                    "name"
                ) == other.compute_environment.dict().get(
                    "name"
                )
        else:
            return NotImplemented

    def launch_pipeline(
        self, seqera: seqeraplatform.SeqeraPlatform, wait: str = "SUBMITTED"
    ) -> dict[str, str]:
        """
        Launch a pipeline.

        Args:
            seqera (seqeraplatform.SeqeraPlatform): A SeqeraPlatform object.
            wait (str, optional): The wait status for the pipeline. Defaults to "SUBMITTED".

        Raises:
            SeqeraKitError: If the pipeline fails to launch.

        Returns:
            dict[str, str]: The launched pipeline.
        """
        # Pre-create some variables to make things easier.
        run_name = "_".join(
            [self.pipeline.name, self.compute_environment.name, date, workflow_uuid]
        )
        profiles = ",".join(self.pipeline.profiles)
        # It's never good to create a path with string handling but it's the quickest way here.
        workdir = "/".join([self.compute_environment.workdir, run_name])

        # Launch the pipeline and wait for submission.
        logging.info(
            f"Launching pipeline {self.pipeline.name} on {self.compute_environment.name}."
        )
        try:
            args = [
                "--workspace",
                self.compute_environment.workspace_id,
                "--compute-env",
                self.compute_environment.ref,
                "--work-dir",
                self.compute_environment.workdir,
                "--name",
                run_name,
                "--wait",
                wait,
                self.pipeline.url,
            ]

            if self.pipeline.profiles != []:
                args.extend(["--profile", profiles])

            launched_pipeline = seqera.launch(
                *args,
                to_json=True,
            )
        except json.decoder.JSONDecodeError as err:
            logging.error(f"Failed to launch pipeline {run_name}.")
            logging.debug(err.doc)
            # Raise pipeline launch error here:
            raise SeqeraKitError(err.doc)
        return launched_pipeline


# Need to use update_forward_refs() to resolve circular references in Pydantic.
LaunchConfig.update_forward_refs()


def parse_args() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
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
        help="List of pipeline and compute environment combinations to exclude. Only checks pipeline and compute environment names.",
    )
    return parser.parse_args()


def read_pipeline_json(path: str) -> list[Pipeline]:
    """
    Read a JSON file of pipeline details.

    Args:
        path (str): The path to the JSON file.

    Returns:
        list[Pipeline]: A list of pipelines read from JSON.
    """
    logging.info("Reading pipeline details...")
    with open(path) as pipeline_file:
        pipelines = json.load(pipeline_file)
    return [Pipeline(**pipeline) for pipeline in pipelines]


def read_compute_env_json(path: str) -> list[ComputeEnvironment]:
    """
    Read a JSON file of compute environment details.

    Args:
        path (str): The path to the JSON file.

    Returns:
        list[ComputeEnvironment]: A list of compute environments read from JSON.
    """
    logging.info("Reading compute environment details...")
    with open(path) as compute_env_file:
        compute_envs = json.load(compute_env_file)
    return [ComputeEnvironment(**compute_env) for compute_env in compute_envs]


def read_launch_configs_from_json_files(file_paths: list[str]) -> list[LaunchConfig]:
    """
    Read a list of JSON files and create LaunchConfig objects.

    Args:
        file_paths (List[str]): The paths to the JSON files.

    Returns:
        List[LaunchConfig]: A list of LaunchConfig objects.
    """
    launch_configs = []
    for file_path in file_paths:
        logging.info(f"Reading launch config file: {file_path}")
        with open(file_path) as json_file:
            in_launch_configs = json.load(json_file)
            for in_launch_config in in_launch_configs:
                launch_config = LaunchConfig(**in_launch_config)
                launch_configs.append(launch_config)
    return launch_configs


def create_launch_config(
    pipelines: list[Pipeline],
    compute_envs: list[ComputeEnvironment],
    include: list[LaunchConfig] = [],
) -> list[LaunchConfig]:
    """
    Create a list of launch configs from a list of pipelines and compute environments.

    Args:
        pipelines (list[Pipeline]): A list of pipelines.
        compute_envs (list[ComputeEnvironment]): A list of compute environments.
        include (list[LaunchConfig], optional): A list of launch configs to include in addition to pipelines * compute envs. Defaults to [].

    Returns:
        list[LaunchConfig]: A list of launch configs.
    """
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
    include: list[LaunchConfig] = [],
    exclude: list[LaunchConfig] = [],
) -> list[LaunchConfig]:
    """
    Filter a list of launch configs by include and exclude lists.

    Args:
        launch_configs (list[LaunchConfig]): A list of initial launch configs.
        include (list[LaunchConfig]): A list of launch configs to include.
        exclude (list[LaunchConfig]): A list of launch configs to exclude.

    Returns:
        list[LaunchConfig]: A list of filtered launch configs.
    """

    logging.info("Adding include launch configs to full set...")
    full_launch_configs = launch_configs + include

    logging.info("Removing exclude launch configs from full set...")
    filtered_launch_configs = [
        launch_config
        for launch_config in full_launch_configs
        if launch_config not in exclude
    ]

    return filtered_launch_configs


def launch_pipelines(
    seqera: seqeraplatform.SeqeraPlatform, launch_configs: list[LaunchConfig]
) -> list[dict[str, str]]:
    """
    Launch a list of pipelines.

    Args:
        seqera (seqeraplatform.SeqeraPlatform): A SeqeraPlatform object.
        launch_configs (list[LaunchConfig]): A list of launch configs.

    Returns:
        list[dict[str, str]]: A list of launched pipelines.
    """
    logging.info("Launching pipelines.")
    launched_pipelines = [
        launch_config.launch_pipeline(seqera=seqera) for launch_config in launch_configs
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
    include = read_launch_configs_from_json_files(args.include) if args.include else []
    exclude = read_launch_configs_from_json_files(args.exclude) if args.exclude else []
    complete_launch_configs = filter_launch_configs(launch_configs, include, exclude)
    launched_pipelines = launch_pipelines(seqera, complete_launch_configs)

    with open(args.output, "w") as output_file:
        json.dump(launched_pipelines, output_file, indent=4)


if __name__ == "__main__":
    main()
