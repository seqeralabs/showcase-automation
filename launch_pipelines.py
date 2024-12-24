import argparse
import datetime
import json
import logging
import pydantic
import tempfile
import uuid
import yaml

from pathlib import Path
from seqerakit import seqeraplatform
from seqerakit.helper import parse_launch_block

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
    config: str | None = None
    pre_run: str | None = None
    revision: str | None = None


class ComputeEnvironment(pydantic.BaseModel):
    """A compute environment to launch a pipeline on."""

    ref: str
    name: str
    workdir: str
    workspace: str
    profiles: list[str] = []


class LaunchConfig(pydantic.BaseModel):
    """A pipeline and compute environment to launch a pipeline on."""

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
        self,
        seqera: seqeraplatform.SeqeraPlatform,
        wait: str = "SUBMITTED",
        launch_container=None,
        labels: str | None = None,
        disable_optimization: bool = False,
    ) -> dict[str, str | bool | None]:
        """
        Launch a pipeline.

        Args:
            seqera (seqeraplatform.SeqeraPlatform): A SeqeraPlatform object.
            wait (str, optional): The wait status for the pipeline. Defaults to "SUBMITTED".
            launch_container (str, optional): The container to launch the pipeline in. Defaults to None.

        Raises:
            SeqeraKitError: If the pipeline fails to launch.

        Returns:
            dict[str, str]: The launched pipeline.
        """
        # Pre-create some variables to make things easier.
        run_name = "_".join(
            [self.pipeline.name, self.compute_environment.ref, date, workflow_uuid]
        )
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

        # Add trailing slash to outdir if it doesn't exist
        if not outdir.endswith("/"):
            outdir += "/"

        # Create params dict
        params = {"outdir": outdir}

        # Launch the pipeline and wait for submission.
        logging.info(
            f"Launching pipeline {self.pipeline.name} on {self.compute_environment.ref}."
        )

        # This should be an object but it's what seqerakit expects.
        args_dict: dict[str, str | bool | dict[str, str] | None] = {
            "workspace": self.compute_environment.workspace,
            "compute-env": self.compute_environment.name,
            "work-dir": workdir,
            "name": run_name,
            "wait": wait,
            "params": params,
            "pipeline": self.pipeline.url,
        }

        if self.pipeline.revision is not None:
            args_dict.update({"revision": self.pipeline.revision})

        if self.pipeline.profiles != [] or self.compute_environment.profiles != []:
            # Create profiles string
            args_dict.update(
                {
                    "profile": str(
                        ",".join(
                            self.pipeline.profiles + self.compute_environment.profiles
                        )
                    )
                }
            )

        if self.pipeline.config is not None:
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".config"
            ) as temp_config_file:
                temp_config_file.write(self.pipeline.config)
                args_dict.update({"config": temp_config_file.name})

        if self.pipeline.pre_run is not None:
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".sh"
            ) as temp_prerun_file:
                temp_prerun_file.write(self.pipeline.pre_run)
                args_dict.update({"pre-run": temp_prerun_file.name})

        if labels is not None:
            args_dict.update({"labels": labels})

        if launch_container is not None:
            args_dict.update({"launch-container": launch_container})

        if disable_optimization:
            args_dict.update({"disable-optimization": True})

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
LaunchConfig.model_rebuild()


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
        "--labels",
        type=str,
        help="Labels to add to the pipeline.",
        required=False,
    )
    parser.add_argument(
        "--pre_run",
        type=str,
        help="Pre-run script to run before launching the pipeline.",
        required=False,
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Config file to use for the pipeline.",
        required=False,
    )
    parser.add_argument(
        "--launch-container",
        type=str,
        help="Container to use for the pipeline.",
        required=False,
    )
    parser.add_argument(
        "-d",
        "--dryrun",
        action="store_true",
        help="Dry run the pipeline launch without actually launching.",
    )
    parser.add_argument(
        "--disable-optimization",
        action="store_true",
        help="Disable optimization of pipeline launches.",
    )
    return parser.parse_args()


def read_yaml(
    paths: list[str], pre_run: str | None = None, config: str | None = None
) -> list[LaunchConfig]:
    """
    Read multiple YAML files of pipeline, compute-env, include and exclude YAML files
    then create a list of launch configs from the resulting mix. Assumes keys 'include',
    'exclude', 'compute-envs' and 'pipelines' are present in the YAML files.

    Args:
        paths (list[str]): The paths to the YAML files.
        pre_run (str, optional): Pre-run script to run before launching the pipeline. Defaults to None.
        config (str, optional): Config file to use for the pipeline. Defaults to None.

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

    # If command line pre-run is enabled, overwrite all pre-run values
    if pre_run is not None:
        for pipeline in pipelines:
            pipeline.pre_run = pre_run

    # If command line config is enabled, overwrite all pre-run values
    if config is not None:
        for pipeline in pipelines:
            pipeline.config = config

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
    pipelines: list[Pipeline], compute_envs: list[ComputeEnvironment]
) -> list[LaunchConfig]:
    """
    Create a list of launch configs from a list of pipelines and compute environments.

    Args:
        pipelines (list[Pipeline]): A list of pipelines.
        compute_envs (list[ComputeEnvironment]): A list of compute environments.

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
    launch_container: str | None = None,
    labels: str | None = None,
    disable_optimization: bool = False,
) -> list[dict[str, str | bool | None]]:
    """
    Launch a list of pipelines.

    Args:
        seqera (seqeraplatform.SeqeraPlatform): A SeqeraPlatform object.
        launch_configs (list[LaunchConfig]): A list of launch configs.
        launch_container (str, optional): The container to launch the pipeline in. Defaults to None.
        labels (str, optional): Labels to add to the pipeline. Defaults to None.
        disable_optimization (bool, optional): Disable optimizations. Defaults to False.

    Returns:
        list[dict[str, str]]: A list of launched pipelines.
    """
    logging.info("Launching pipelines.")
    launched_pipelines = [
        launch_config.launch_pipeline(
            seqera=seqera,
            launch_container=launch_container,
            labels=labels,
            disable_optimization=disable_optimization,
        )
        for launch_config in launch_configs
    ]
    logging.info("Pipelines launched.")
    return launched_pipelines


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO)

    seqera = seqeraplatform.SeqeraPlatform(dryrun=args.dryrun)

    complete_launch_configs = read_yaml(args.inputs, args.pre_run, args.config)

    launched_pipelines = launch_pipelines(
        seqera,
        complete_launch_configs,
        args.launch_container,
        args.labels,
        args.disable_optimization,
    )

    logging.info(f"Writing launches to JSON file {args.output}")
    with open(args.output, "w") as output_file:
        json.dump(launched_pipelines, output_file, indent=4)


if __name__ == "__main__":
    main()
