locals {
  bignbit_appname = "bignbit"
}

provider "aws" {
  alias = "bignbit-aws"
  default_tags {
    tags = merge(local.default_tags, {
      application = local.bignbit_appname,
      Version     = var.app_version,
    })
  }
}

module "bignbit_module" {
  source = "../../terraform"
  providers = {
    aws = aws.bignbit-aws
  }

  count = 1

  stage  = var.bignbit_stage
  cmr_environment = var.bignbit_cmr_environment
  prefix = var.prefix

  data_buckets = [aws_s3_bucket.protected.id, aws_s3_bucket.public.id, aws_s3_bucket.private.id]

  config_bucket = aws_s3_bucket.internal.bucket
  config_dir    = "big-config"

  bignbit_audit_bucket = aws_s3_bucket.internal.bucket
  bignbit_audit_path   = "bignbit-cnm-output"

  gibs_region     = var.gibs_region == "mocked" ? "us-west-2" : var.gibs_region
  gibs_queue_name = var.gibs_queue_name == "mocked" ? aws_sqs_queue.gitc_input_queue[0].name : var.gibs_queue_name
  gibs_account_id = var.gibs_account_id == "mocked" ? local.account_id : var.gibs_account_id

  edl_user_ssm = var.edl_user
  edl_pass_ssm = var.edl_pass

  permissions_boundary_arn = var.permissions_boundary_arn
  security_group_ids = []
  subnet_ids = []

  app_name = local.bignbit_appname
  default_tags = merge(local.default_tags, {
    application = local.bignbit_appname,
    Version     = var.app_version
  })
  lambda_container_image_uri = var.lambda_container_image_uri

  lambda_timeouts = {
    "get_dataset_configuration" = 30
    "get_granule_umm_json" = 30
    "get_collection_concept_id" = 30
    "identify_image_file" = 30
    "submit_harmony_job" = 60
    "handle_big_result" = 300
    "get_harmony_job_status" = 60
    "apply_opera_hls_treatment" = 30
    "send_to_gitc" = 15
    "handle_gitc_response" = 45
  }

  lambda_memory_sizes = {
    "get_dataset_configuration" = 256
    "get_granule_umm_json" = 256
    "get_collection_concept_id" = 256
    "identify_image_file" = 256
    "submit_harmony_job" = 256
    "handle_big_result" = 1024
    "get_harmony_job_status" = 256
    "apply_opera_hls_treatment" = 512
    "send_to_gitc" = 256
    "handle_gitc_response" = 256
  }
}

/*

In a typical cumulus installation, this is how you would define the workflow:

module "browse_image_workflow" {
  source          = "https://github.com/nasa/cumulus/releases/download/v16.1.2/terraform-aws-cumulus-workflow.zip"
  prefix          = var.prefix
  name            = "BrowseImageWorkflow"
  workflow_config = module.cumulus.workflow_config
  system_bucket   = var.system_bucket
  tags            = merge(local.tags, { application = "BrowseImageWorkflow" })

  state_machine_definition = module.bignbit_module.workflow_definition
}
*/

# This example is deployable without cumulus installed for the purpose of testing the module in isolation; so it does not use terraform-aws-cumulus-workflow.zip
resource "aws_sfn_state_machine" "sfn_state_machine" {
  name     = "${local.ec2_resources_name}-BrowseImageWorkflow"
  role_arn = aws_iam_role.step.arn

  definition = module.bignbit_module[0].workflow_definition
}
