# Seqera Labs Showcase Automation Scripts

## Overview

This repository contains automation scripts for launching and collecting information from pipelines in the Seqera Labs platform.

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

This script performs the following steps:

- Queries the Seqera Platform API to retrieve information about Data Studios in selected workspaces (`--workspaces`)
- Can filter by workspace IDs and status
- Supports sending results to Slack

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
    workspace: '138659136604200'
    profiles: []  # Default profiles for all pipelines
```

Example with profile mappings (for Slurm with Singularity):

```yaml
compute-envs:
  - ref: slurm
    name: seqera_slurm
    workdir: /home/seqera/work
    workspace: '138659136604200'
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
      workspace: '138659136604200'
```

#### `exclude`

This file removes pipeline and compute environment combinations. It has the same format as the include YAML but removes existing combinations before running. This is applied after the include YAML.

### Automated Running

An implementation of these two steps in GitHub Actions is included in [./.github/workflows/seqera-showcase.yml](./.github/workflows/seqera-showcase.yml). In this workflow, the first job (`launch`) launches the pipelines, and the subsequent job (`clearup-and-delete`) runs the second process after a pre-defined wait period, implemented via a [GitHub Deployment Environment](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment). It uses a GitHub Action artifact to transfer the JSON file between jobs.
