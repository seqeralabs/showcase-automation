import argparse
import datetime
import json
import logging
import pydantic
import uuid
import yaml

from pathlib import Path
from seqerakit import seqeraplatform
from seqerakit.helper import parse_launch_block
import yaml

## Globals
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

    ref: str
    name: str
    workdir: str
    workspace: str


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
                return self.pipeline.model_dump().get(
                    "name"
                ) == other.pipeline.model_dump().get(
                    "name"
                ) and self.compute_environment.model_dump().get(
                    "name"
                ) == other.compute_environment.model_dump().get(
                    "name"
                )
        else:
            return NotImplemented

    def launch_pipeline(
        self, seqera: seqeraplatform.SeqeraPlatform, wait: str = "SUBMITTED"
    ) -> dict[str, str | bool | None]:
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
            [self.pipeline.name, self.compute_environment.ref, date, workflow_uuid]
        )
        profiles = ",".join(self.pipeline.profiles)
        # It's never good to create a path with string handling but it's the quickest way here.
        workdir = "/".join(
            [self.compute_environment.workdir, self.pipeline.name, "work-" + date]
        )

        outdir = "/".join(
            [
                self.compute_environment.workdir,
                self.pipeline.name,
                "results-test-" + date,
            ]
        )

        # Create params dict
        params = {"outdir": outdir}

        # Launch the pipeline and wait for submission.
        logging.info(
            f"Launching pipeline {self.pipeline.name} on {self.compute_environment.ref}."
        )

        args_dict = {
            "workspace": self.compute_environment.workspace,
            "compute-env": self.compute_environment.name,
            "work-dir": workdir,
            "name": run_name,
            "wait": wait,
            "params": params,
            "pipeline": self.pipeline.url,
        }

        if self.pipeline.profiles != []:
            args_dict.update({"profile": profiles})

        default_response = {
            "workflowId": None,
            "workflowUrl": None,
            "workspaceId": None,
            "workspaceRef": None,
            "workflowName": run_name,
            "computeEnvironment": self.compute_environment.name,
            "launchSuccess": False,
            "error": "",
        }

        try:
            # Use seqerakit helper function to construct arguments
            args_list = parse_launch_block(args_dict)
            launched_pipeline = seqera.launch(*args_list, to_json=True)

            # If dryrun, return default response
            if seqera.dryrun:
                return default_response

        # If we fail to add the pipeline for a predictable reason we can log and continue
        except seqeraplatform.ResourceCreationError as err:
            logging.info(
                f"Failed to launch pipeline {run_name}. Logging and proceeding..."
            )
            message = "\n".join(err.args)
            logging.debug(message)
            # Raise pipeline launch error here:
            default_response.update({"error": message})
            return default_response

        # If we fail to add the pipeline for an unpredictable reason we log and fail
        except json.decoder.JSONDecodeError as err:
            logging.error(f"Failed to launch pipeline {run_name}.")
            logging.debug(err.doc)
            # Raise pipeline launch error here:
            raise SeqeraKitError(err.doc)

        # Add pipeline launch info to dict
        launched_pipeline.update(
            {
                "workflowName": run_name,
                "computeEnvironment": self.compute_environment.name,
                "launchSuccess": True,
                "error": "",
            }
        )
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
        "-i",
        "--inputs",
        nargs="+",
        required=True,
        type=Path,
        help="The input yaml files to read. Must contain keys 'include', 'exclude', 'compute-envs' and 'pipelines'.",
    )
    parser.add_argument(
        "-d",
        "--dryrun",
        action="store_true",
        help="Dry run the pipeline launch without actually launching.",
    )
    return parser.parse_args()


def read_yaml(paths: list[str]) -> list[LaunchConfig]:
    """
    Read multiple YAML files of pipeline, compute-env, include and exclude YAML files
    then create a list of launch configs from the resulting mix. Assumes keys 'include',
    'exclude', 'compute-envs' and 'pipelines' are present in the YAML files.

    Args:
        paths (list[str]): The paths to the YAML files.

    Returns:
        list[Pipeline]: A list of pipelines read from YAML.
    """
    logging.info("Reading launch details...")

    # Pre-populate empty output to fill
    # This saves us doing lots of if/or statements for getting the contents
    objects: dict = {"pipelines": [], "compute-envs": [], "include": [], "exclude": []}

    for path in paths:
        with open(path) as pipeline_file:
            # Extend existing dictionary values (lists)
            # We grab keys from pre-populated dict so we can do some key checking
            file_contents = yaml.safe_load(pipeline_file)
            for key in file_contents.keys():
                if key in objects.keys():
                    objects[key] = objects[key] + file_contents[key]
                else:
                    raise KeyError(f"Unexpected key in YAML file: {key}")

    # Get pipeline details from 'pipelines' key
    pipelines = [Pipeline(**pipeline) for pipeline in objects["pipelines"]]

    # Get compute env details from 'compute-envs' key
    compute_envs = [
        ComputeEnvironment(**compute_env) for compute_env in objects["compute-envs"]
    ]

    # Get include and exclude details from 'include' and 'exclude' keys
    include = [LaunchConfig(**include) for include in objects["include"]]
    exclude = [LaunchConfig(**exclude) for exclude in objects["exclude"]]

    # Create matrix of pipeline * compute-envs to LaunchConfigs
    launch_configs = create_launch_config(pipelines, compute_envs)

    # Add any included LaunchConfigs and remove excluded LaunchConfigs
    complete_launch_configs = filter_launch_configs(launch_configs, include, exclude)

    return complete_launch_configs


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
    seqera: seqeraplatform.SeqeraPlatform,
    launch_configs: list[LaunchConfig],
) -> list[dict[str, str | bool | None]]:
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

    seqera = seqeraplatform.SeqeraPlatform(dryrun=args.dryrun)

    complete_launch_configs = read_yaml(args.inputs)

    launched_pipelines = launch_pipelines(seqera, complete_launch_configs)

    logging.info(f"Writing launches to JSON file {args.output}")
    with open(args.output, "w") as output_file:
        json.dump(launched_pipelines, output_file, indent=4)


if __name__ == "__main__":
    main()
