terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.100"
      # The staging bucket must live in a fixed region (us-west-2 for Harmony) that is
      # independent of the region used by the rest of the stack. AWS provider v5.x cannot
      # set a per-resource region, so the consumer must pass an aliased provider pinned to
      # that region. (A per-resource `region` argument only exists in provider v6+.)
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

}
