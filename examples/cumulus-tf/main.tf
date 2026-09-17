terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.100, < 6.0.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.0"
    }
  }
}

provider "aws" {
  ignore_tags {
    key_prefixes = ["gsfc-ngap"]
  }

  default_tags {
    tags = local.default_tags
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}


locals {
  environment = var.bignbit_stage

  # This is the convention we use to know what belongs to each other
  ec2_resources_name = terraform.workspace == "default" ? var.prefix : "${var.prefix}-${terraform.workspace}"

  # Account ID used for getting the ECR host
  account_id = data.aws_caller_identity.current.account_id

  current_aws_region = data.aws_region.current.name

  default_tags = {
    team: "TVA",
    application: local.ec2_resources_name,
    Environment = var.bignbit_stage,
  }
}
