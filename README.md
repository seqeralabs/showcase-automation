# Seqera Labs Showcase Automation Scripts

## Overview

This repository contains automation scripts for launching and collecting information from pipelines in the Seqera Labs platform.

Install the locked dependencies with `uv sync --locked`, then run scripts with
`uv run --locked python <script>.py`.

### `launch_pipelines.py`

This script performs the following steps:

- Reads pipeline details from multiple YAML files in [`./pipelines/`](./pipelines/).
- Reads compute environment details from multiple files in [`./compute-envs/`](./compute-envs/).
- Creates combinations of pipelines and compute environments in an all-by-all manner.
- Includes YAML files specified in [`include/`](./include/) as both pipelines and compute environments.
- Excludes YAML files specified in [`exclude/`](./exclude/) based on pipeline and compute environment names.
- Launches all pipelines generated from this data.
- Logs any "failure to launch" without causing the program to fail.
- Writes all pipelines to a YAML file for subsequent steps.

### `collect_metadata.py`

This script performs the following steps:

- Uses the JSON from the previous step.
- Reads workspace and workflow ID from JSON produced in the first step.
- Utilizes `tw runs dump` to download relevant information.
- Creates a JSON file containing all pipeline run information.
- Sends a message to Slack (`--slack`) in the specified channel (`--slack_channel`) with the compressed JSON as an attachment.
- If `--delete` is enabled, it removes the pipeline if it has successfully completed.
- If `--force` is enabled, it removes the pipeline even if it has not finished or failed.

### `studios_api_test.py`

This script performs the following steps:

- Queries the Seqera Platform API to retrieve information about Data Studios in selected workspaces (`--workspaces`)
- Can filter by workspace IDs and status
- Supports sending results to Slack

### `studios_autotest.py`

This script creates, soak-tests, stops and deletes Studios. It has two sub-commands that mirror
`launch_pipelines.py` and `extract_metadata.py`, and drives Studios through the `tw` CLI.

`launch`:

- Reads Studio definitions from YAML files in [`./studios/`](./studios/).
- Resolves an unpinned template (repository without a tag) to the newest `recommended` template available in the workspace.
- Creates one Studio per definition with a unique name, auto-starts it and waits until it is `running` (`--startup-timeout`, default 30 minutes).
- Logs any failure to create or start a Studio without causing the program to fail.
- Writes all launch records to a JSON file for the `finish` step.

`finish`:

- Reads the launch records written by `launch`.
- Checks each Studio is still `running` after the soak period, then stops it and waits until it is `stopped` (`--stop-timeout`, default 20 minutes).
- If `--delete` is enabled, deletes the Studio once it is `stopped` (or `errored`). If `--force` is enabled, attempts the deletion whatever the status.
- Classifies each Studio as `PASSED`, `FAILED_TO_CREATE`, `FAILED_TO_START`, `FAILED_SOAK` or `FAILED_TO_STOP`.
- Writes a results table to the GitHub Actions job summary when run in Actions. Slack notifications are left out until the workflow is confirmed to work.
- If `--fail-on-error` is enabled, exits with a non-zero code when any Studio did not pass.

### Input YAML Files

#### `pipelines`

Each entry in the YAML must specify a list of pipelines to launch, with the following fields:

- `name` (string): User-readable name of the launched workflow.
- `url` (string): The URL of the repository or pipeline name in the workspace.
- `latest` (bool): Pull the latest version specified by revision (required).
- `profiles` (List of strings): Profiles to apply to the pipeline run. Use an empty list to mean no profile.

Example:

```yaml
pipelines:
  - name: hello
    url: hello
    latest: true
    profiles: []
```

#### `compute-envs`

Each entry in the YAML must specify an existing compute environment in the Seqera platform workspace, with the following fields:

- `ref` (string): User-readable name of the compute environment.
- `name` (string): The name of the compute environment in the Seqera platform.
- `workdir` (string): The work directory to use for the compute environment. A subdirectory will be created per pipeline run.
- `workspace` (string): The ID of the workspace the compute environment belongs to.
- `profiles` (List of strings, optional): Default profiles to apply to all pipelines on this compute environment. Defaults to empty list.
- `profile_mappings` (List of profile mappings, optional): Pipeline-specific profile configurations. Each mapping contains:
  - `pipelines` (List of strings): Pipeline names or glob patterns (e.g., "nf-core-*" matches all nf-core pipelines)
  - `profiles` (List of strings): Profiles to apply when a pipeline matches the pattern

Example:

```yaml
compute-envs:
  - ref: aws
    name: seqera_aws_ireland_fusionv2_nvme
    workdir: s3://seqera-showcase
    workspace: ''
    profiles: []  # Default profiles for all pipelines
```

Example with profile mappings (for Slurm with Singularity):

```yaml
compute-envs:
  - ref: slurm
    name: seqera_slurm
    workdir: /home/seqera/work
    workspace: ''
    profiles: []  # Default profiles for pipelines without specific mappings
    profile_mappings:
      # Apply singularity profile to nf-core pipelines
      - pipelines: ["nf-core-*", "rnaseq", "sarek"]
        profiles: ["singularity"]
      # Hello pipeline runs without singularity
      - pipelines: ["hello"]
        profiles: []
```

**Note on Profile Mappings**: Profile mappings are useful when different pipelines require different profiles on the same compute environment. For example, some pipelines may not include certain profiles in their `nextflow.config`, and Nextflow 24.05+ will fail if you try to use a non-existent profile. Use profile mappings to conditionally apply profiles only to pipelines that support them.

#### `studios`

Each entry in the YAML must specify a Studio to create on an existing compute environment, with the following fields:

- `ref` (string): User-readable name of the compute environment, used in the Studio name.
- `name` (string): Base name of the Studio. The date and a run UUID are appended to make it unique.
- `workspace` (string): The ID of the workspace the compute environment belongs to.
- `compute_env` (string): The name of the compute environment in the Seqera platform. Studios support AWS Cloud, Azure Cloud, Google Cloud and AWS Batch (without Fargate) compute environments.
- `template` (string): Template image, e.g. `public.cr.seqera.io/platform/data-studio-jupyter:4.6.0-0.12`. Omit the tag to use the newest `recommended` template available in the workspace.
- `description` (string, optional): Studio description. A link to the GitHub Actions run is appended when available.
- `cpu`, `memory`, `gpu` (int, optional): Resources allocated to the session. Default to 2 CPUs, 8192 MiB and 0 GPUs.
- `lifespan` (int, optional): Hours after which the Platform stops the session on its own. Acts as a safety net if the cleanup job never runs, so keep it longer than the soak period.
- `mount_data_uris` (List of strings, optional): Data-link URIs to mount, e.g. `s3://seqera-showcase`.
- `private` (bool, optional): Create a private Studio. Defaults to `false`.

Example:

```yaml
studios:
  - ref: aws-cloud
    name: jupyter
    workspace: "14715071736572"
    compute_env: seqera_aws_cloud
    template: public.cr.seqera.io/platform/data-studio-jupyter
    cpu: 2
    memory: 8192
    gpu: 0
    lifespan: 2
```

#### `include`

This file is made of a list of complete configurations, each containing a pipeline and compute environment that match the above files.

Example:

```yaml
include:
  - pipeline:
      name: sentieon
      url: nf-sentieon
      latest: true
      profiles:
        - test
    compute_environment:
      ref: aws
      name: seqera_aws_ireland_fusionv2_nvme
      workdir: s3://seqera-showcase
      workspace: ''
```

#### `exclude`

This file removes pipeline and compute environment combinations. It has the same format as the include YAML but removes existing combinations before running. This is applied after the include YAML.

### Automated Running

An implementation of these two steps in GitHub Actions is included in [./.github/workflows/seqera-showcase.yml](./.github/workflows/seqera-showcase.yml). In this workflow, the first job (`launch`) launches the pipelines, and the subsequent job (`clearup-and-delete`) runs the second process after a pre-defined wait period, implemented via a [GitHub Deployment Environment](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment). It uses a GitHub Action artifact to transfer the JSON file between jobs.

The same two-job pattern is used for Studios in [./.github/workflows/seqera-showcase-studios-staging.yml](./.github/workflows/seqera-showcase-studios-staging.yml). The `launch` job creates the Studios declared in `studios/staging*.yaml` and waits until they are running; the `soak-stop-and-delete` job always runs after the soak period selected with the `timer` input, stops and deletes the Studios, writes the results table to the job summary and fails the run if any Studio did not pass.
