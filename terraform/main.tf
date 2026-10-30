terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.100"

      # A separate provider for the staging bucket to be placed in the us-west-2 region for Harmony compatibility
      # independent of other resources. This is a workaround for Terraform's 5.x.x AWS provider which doesn't allow
      # for per-resource regions and should be removed once we upgrade to 6.x.x. See 
      configuration_aliases = [aws.staging]
    }
    null = {
      source  = "hashicorp/null"
      version = "> 2.1"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}


locals {
  environment = var.stage
  cmr_environment = var.cmr_environment == "" ? (upper(var.stage) == "OPS" ? "OPS" : "UAT") : upper(var.cmr_environment)

  account_id = data.aws_caller_identity.current.account_id

  aws_resources_name = terraform.workspace == "default" ? "${var.prefix}-${var.app_name}" : "${var.prefix}-${var.app_name}-${terraform.workspace}"
  sns_topic_name = terraform.workspace == "default" ? "${var.app_name}-${var.prefix}" : "${var.app_name}-${var.prefix}-${terraform.workspace}"

  default_tags = length(var.default_tags) == 0 ? {
    team : "TVA",
    application : var.app_name,
    Environment = var.stage
  } : var.default_tags

  current_aws_region = data.aws_region.current.name

  gibs_sqs_region = var.create_gibs_mock_queue ? data.aws_region.current.name : var.gibs_region
  gibs_sqs_url    = var.create_gibs_mock_queue ? aws_sqs_queue.gibs_mock_queue[0].url : "https://sqs.${var.gibs_region}.amazonaws.com/${var.gibs_account_id}/${var.gibs_queue_name}"

}
