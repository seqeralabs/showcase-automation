# Showcase Automation Scripts for Seqera Labs

## Overview

This repo contains two scripts which launch and collect the pipelines respectively.

- `launch_pipelines.py`:
  - Read in pipeline details from multiple YAML (see [`./pipelines/`](./pipelines/))
  - Read in multiple compute environment files (see [`./compute-envs/`](./compute-envs/))
  - Make a combination of both, i.e. an all-by-all
  - Add the --include YAML files which are pipelines and compute-envs (see [`include/`](./include/))
  - Remove any that match the pipeline and compute env names in the --exclude YAML files (see [`exclude/`](./exclude/))
  - Launch all pipelines created from this data
  - "Failure to launch" is caught and logged but does not cause the program to fail
  - All pipelines are written to a YAML file which is created as an object to use in subsequent steps

- `collect_metadata.py`:
  - Use the JSON from the previous step
  - Reads in the workspace and workflow ID from JSON produced in step 1
  - Uses `tw runs dump` to download the relevant information
  - Creates a JSON composed of all pipeline run info
  - Writes this JSON to a file.
  - Send a message to Slack (`--slack`) at Slack channel `--slack_channel` and include the compressed JSON as an attachment.
  - If `--delete` is enabled it will remove the pipeline if it has successfully completed
  - If `--force` is enabled it will remove the pipeline _even if it has not finished or failed_

### Input YAML files

You should specify at least 1 YAML file containing valid inputs when you run `launch_pipelines.py` (`-i`). Each YAML file must contain at least 1 pipeline and compute environment definition to launch a pipeline. 

#### `pipelines`

The pipeline entry in the YAML must specify a list of `pipelines` to launch, each element in the list must contain the following fields:

- `name` (string): User readable name of the launched workflow.
- `url` (string): The URL of the repository or pipeline name in the workspace.
- `latest` (bool): Pull the latest version specified by revision (required).
- `profiles` (List of strings): Profiles to apply to the pipeline run. Use empty list to mean no profile.

```yaml
pipelines:
  - name: hello
    url: hello
    latest: true
    profiles: []
```

Specify multiple using a list of pipelines:

```yaml
pipelines:
  - name: rnaseq
    url: nf-core-rnaseq
    latest: true
    profiles:
    - test
  - name: sarek
    url: nf-core-sarek
    latest: true
    profiles:
    - test
```

#### `compute-envs`

The compute environment entry in the YAML must specify a list of existing compute environment in the Seqera platform workspace. It must contain the following fields:

- `ref` (string): User readable name of the compute environment.
- `name` (string): The name of the compute environment in the Seqera platform
- `workdir` (string): The work directory to use for the compute environment. A subdirectory will be created per pipeline run.
- `workspace` (string): The ID of the workspace the compute environment belongs to.

```yaml
compute-envs:
  - ref: aws
    name: seqera_aws_ireland_fusionv2_nvme
    workdir: s3://seqeralabs-showcase
    workspace: '138659136604200'
```

Like with pipelines, you can specify multiple as a list:

```yaml
compute-envs:
  - ref: aws
    name: seqera_aws_ireland_fusionv2_nvme
    workdir: s3://seqeralabs-showcase
    workspace: '138659136604200'
  - ref: azure
    name: seqera_azure_virginia_fusion
    workdir: az://seqeralabs-showcase
    workspace: '138659136604200'
  - ref: gcp
    name: seqera_gcp_finland_fusion
    workdir: gs://seqeralabs-showcase-eu-north-1
    workspace: '138659136604200'
```

#### `include`

All pipelines and compute environments are combined on an all-by-all basis. If you wish to include additional pipeline and compute environment combinations, you can use the include YAML file. This will create additional pipeline and compute environment combinations to run. The include.yaml is made of a list of complete configurations, with each object containing a pipeline and compute environment which match the above files:

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
    workdir: s3://seqeralabs-showcase
    workspace: '138659136604200'
```

Conversely, an exclude file can be used to remove pipeline and compute environment combinations. An exclude YAML has the same format as the include YAML but removes any existing combinations before running. This is applied after the include YAML so you can effectively remove a combination added with an include file.

Both `launch_pipelines.py` and `extract_metadata.py` take multiple input YAML files. Therefore you can mix and match certain combinations on the command line:

```bash
python launch_pipelines.py \
    -i pipeline1.yaml pipeline2.yaml \
    compute-env1.yaml compute-env2.yaml \
    include1.yaml include2.yaml \
    exclude1.yaml exclude2.yaml 
```

## Automated running

An implementation of the two steps in Github Actions [are included](./.github/workflows/seqera-showcase.yml). In this workflow, we launch the pipelines in job 1 (`launch`) and the following job, `clearup-and-delete` runs the second process after a pre-defined wait period which is implemented via a [Github Deployment Environment](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment). It uses a Github Action artifact to transfer the JSON file between jobs.
