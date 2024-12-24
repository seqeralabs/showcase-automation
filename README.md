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

Example:

```yaml
compute-envs:
  - ref: aws
    name: seqera_aws_ireland_fusionv2_nvme
    workdir: s3://seqera-showcase
    workspace: '138659136604200'
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
      workspace: '138659136604200'
```

#### `exclude`

This file removes pipeline and compute environment combinations. It has the same format as the include YAML but removes existing combinations before running. This is applied after the include YAML.

### Automated Running

An implementation of these two steps in GitHub Actions is included in [./.github/workflows/seqera-showcase.yml](./.github/workflows/seqera-showcase.yml). In this workflow, the first job (`launch`) launches the pipelines, and the subsequent job (`clearup-and-delete`) runs the second process after a pre-defined wait period, implemented via a [GitHub Deployment Environment](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment). It uses a GitHub Action artifact to transfer the JSON file between jobs.
