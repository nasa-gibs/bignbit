data "aws_ecr_authorization_token" "token" {}

locals {
  lambda_container_image_uri_split = split("/", var.lambda_container_image_uri)
  ecr_image_name_and_tag = split(":", element(local.lambda_container_image_uri_split, length(local.lambda_container_image_uri_split) - 1))
  ecr_image_name = "${local.aws_resources_name}-${element(local.ecr_image_name_and_tag, 0)}"
  ecr_image_tag = element(local.ecr_image_name_and_tag, 1)

  # Truncate all function names to max 64 characters for AWS Lambda
  get_dataset_configuration_function_name = substr("${local.aws_resources_name}-get_dataset_configuration", 0, 64)
  get_granule_umm_json_function_name = substr("${local.aws_resources_name}-get_granule_umm_json", 0, 64)
  get_collection_concept_id_function_name = substr("${local.aws_resources_name}-get_collection_concept_id", 0, 64)
  identify_image_file_function_name = substr("${local.aws_resources_name}-identify_image_file", 0, 64)
  submit_harmony_job_function_name = substr("${local.aws_resources_name}-submit_harmony_job", 0, 64)
  handle_big_result_function_name = substr("${local.aws_resources_name}-handle_big_result", 0, 64)
  get_harmony_job_status_function_name = substr("${local.aws_resources_name}-get_harmony_job_status", 0, 64)
  apply_opera_hls_treatment_function_name = substr("${local.aws_resources_name}-apply_opera_hls_treatment", 0, 64)
  send_to_gitc_function_name = substr("${local.aws_resources_name}-send_to_gitc", 0, 64)
  handle_gitc_response_function_name = substr("${local.aws_resources_name}-handle_gitc_response", 0, 64)
}


resource aws_ecr_repository "lambda-image-repo" {
  name         = local.ecr_image_name
  force_delete = var.force_delete_ecr
}


resource null_resource ecr_login {
  triggers = {
    image_uri = var.lambda_container_image_uri
    # Re-run whenever the target ECR repo changes. Without this, a recreated or
    # emptied repo is not detected (triggers only saw image_uri), so the login
    # and push below are skipped and Lambda ends up pointing at a missing image.
    ecr_repo = aws_ecr_repository.lambda-image-repo.repository_url
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-e", "-c"]
    command = <<EOF
      docker login ${data.aws_ecr_authorization_token.token.proxy_endpoint} -u AWS -p ${data.aws_ecr_authorization_token.token.password}
      EOF
  }
}


resource null_resource upload_ecr_image {
  depends_on = [null_resource.ecr_login]
  triggers = {
    image_uri = var.lambda_container_image_uri
    # Tie the push to the ECR repo identity so a recreated/empty repo forces a
    # re-push instead of being skipped on an unchanged image_uri.
    ecr_repo = aws_ecr_repository.lambda-image-repo.repository_url
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-e", "-c"]
    command = <<EOF
      if [ -z "$(docker images -q ${var.lambda_container_image_uri} 2> /dev/null)" ]; then
        docker pull ${var.lambda_container_image_uri}
      fi
      docker tag ${var.lambda_container_image_uri} ${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}
      docker push ${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}
      EOF
  }
}


# This doesn't work in terraform 0.13.x because it tries to resolve the image during the plan phase instead of the
# apply phase. Once IA updates to a newer terraform version, this can be used instead of having every lambda
# function depend on the null_resource.upload_ecr_image resource.
#data aws_ecr_image lambda_image {
#  depends_on = [
#    null_resource.upload_ecr_image
#  ]
#  repository_name = aws_ecr_repository.lambda-image-repo.name
#  image_tag       = local.ecr_image_tag
#
#}


resource "aws_lambda_function" "get_dataset_configuration" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.get_dataset_configuration.lambda_handler"]
  }
  function_name = local.get_dataset_configuration_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      STACK_NAME                  = var.prefix
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "get_granule_umm_json" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.get_granule_umm_json.lambda_handler"]
  }
  function_name = local.get_granule_umm_json_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      STACK_NAME                  = var.prefix
      EDL_USER_SSM                = var.edl_user_ssm
      EDL_PASS_SSM                = var.edl_pass_ssm
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "get_collection_concept_id" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.get_collection_concept_id.lambda_handler"]
  }
  function_name = local.get_collection_concept_id_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      STACK_NAME                  = var.prefix
      EDL_USER_SSM                = var.edl_user_ssm
      EDL_PASS_SSM                = var.edl_pass_ssm
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "identify_image_file" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.identify_image_file.lambda_handler"]
  }
  function_name = local.identify_image_file_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      STACK_NAME                  = var.prefix
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "submit_harmony_job" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.submit_harmony_job.lambda_handler"]
  }
  function_name = local.submit_harmony_job_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      STACK_NAME                  = var.prefix
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
      EDL_USER_SSM                = var.edl_user_ssm
      EDL_PASS_SSM                = var.edl_pass_ssm
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "handle_big_result" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.handle_big_result.lambda_handler"]
  }
  function_name = local.handle_big_result_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 300
  memory_size   = 1024

  environment {
    variables = {
      STACK_NAME                  = var.prefix
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
      EDL_USER_SSM                = var.edl_user_ssm
      EDL_PASS_SSM                = var.edl_pass_ssm
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "get_harmony_job_status" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.get_harmony_job_status.lambda_handler"]
  }
  function_name = local.get_harmony_job_status_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      STACK_NAME                  = var.prefix
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
      EDL_USER_SSM                = var.edl_user_ssm
      EDL_PASS_SSM                = var.edl_pass_ssm
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "apply_opera_hls_treatment" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.apply_opera_hls_treatment.lambda_handler"]
  }
  function_name = local.apply_opera_hls_treatment_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 30
  memory_size   = 512

  environment {
    variables = {
      STACK_NAME                  = var.prefix
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
      EDL_USER_SSM                = var.edl_user_ssm
      EDL_PASS_SSM                = var.edl_pass_ssm
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "send_to_gitc" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.send_to_gitc.lambda_handler"]
  }
  function_name = local.send_to_gitc_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 15
  memory_size   = 128

  environment {
    variables = {
      STACK_NAME                  = local.aws_resources_name
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
      GIBS_REGION                 = local.gibs_sqs_region
      GIBS_SQS_URL                = local.gibs_sqs_url
      GIBS_RESPONSE_TOPIC_ARN     = aws_sns_topic.gibs_response_topic.arn
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


resource "aws_lambda_function" "handle_gitc_response" {
  depends_on = [
    null_resource.upload_ecr_image
  ]

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda-image-repo.repository_url}:${local.ecr_image_tag}"
  image_config {
    command = ["bignbit.handle_gitc_response.handler"]
  }
  function_name = local.handle_gitc_response_function_name
  role          = aws_iam_role.bignbit_lambda_role.arn
  timeout       = 45
  memory_size   = 128

  environment {
    variables = {
      STACK_NAME                  = local.aws_resources_name
      CUMULUS_MESSAGE_ADAPTER_DIR = "/opt/"
      REGION                      = data.aws_region.current.name
      BIGNBIT_AUDIT_BUCKET_NAME   = var.bignbit_audit_bucket
      BIGNBIT_AUDIT_PATH_NAME     = var.bignbit_audit_path
      CMR_ENVIRONMENT             = local.cmr_environment
      EDL_USER_SSM                = var.edl_user_ssm
      EDL_PASS_SSM                = var.edl_pass_ssm
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

}


# Lambda IAM setup below


data "aws_iam_policy_document" "bignbit_lambda_assume_role_policy" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_ssm_parameter" "ed-user" {
  name = var.edl_user_ssm
}
data "aws_ssm_parameter" "ed-pass" {
  name = var.edl_pass_ssm
}

resource "aws_iam_role" "bignbit_lambda_role" {
  name                 = "${local.aws_resources_name}-lambda-role"
  assume_role_policy   = data.aws_iam_policy_document.bignbit_lambda_assume_role_policy.json
  permissions_boundary = var.permissions_boundary_arn
}

data "aws_iam_policy_document" "bignbit_lambda_policy" {
  statement {
    actions = [
      "ec2:CreateNetworkInterface",
      "sns:publish",
      "cloudformation:DescribeStacks",
      "dynamodb:ListTables",
      "ec2:DeleteNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "events:DeleteRule",
      "events:DescribeRule",
      "events:DisableRule",
      "events:EnableRule",
      "events:ListRules",
      "events:PutRule",
      "kinesis:DescribeStream",
      "kinesis:GetRecords",
      "kinesis:GetShardIterator",
      "kinesis:ListStreams",
      "kinesis:PutRecord",
      "lambda:GetFunction",
      "lambda:invokeFunction",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
      "s3:ListAllMyBuckets",
      "sns:List*",
      "states:DescribeActivity",
      "states:DescribeExecution",
      "states:GetActivityTask",
      "states:GetExecutionHistory",
      "states:ListStateMachines",
      "states:SendTaskFailure",
      "states:SendTaskSuccess",
      "states:StartExecution",
      "states:StopExecution"
    ]
    resources = ["*"]
  }

  statement {
    actions = [
      "s3:GetAccelerateConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:GetReplicationConfiguration",
      "s3:GetBucket*",
      "s3:PutAccelerateConfiguration",
      "s3:PutLifecycleConfiguration",
      "s3:PutReplicationConfiguration",
      "s3:PutBucket*",
      "s3:ListBucket*",
      "s3:AbortMultipartUpload",
      "s3:GetObject*",
      "s3:PutObject*",
      "s3:ListMultipartUploadParts",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = [
      "arn:aws:s3:::${local.staging_bucket_name}",
      "arn:aws:s3:::${var.config_bucket}",
      "arn:aws:s3:::${var.bignbit_audit_bucket}",
      "arn:aws:s3:::${local.staging_bucket_name}/*",
      "arn:aws:s3:::${var.config_bucket}/*",
      "arn:aws:s3:::${var.bignbit_audit_bucket}/*"
    ]
  }

  statement {
    actions = [
      "ssm:GetParameters",
      "ssm:GetParameter"
    ]
    resources = [
      data.aws_ssm_parameter.ed-user.arn,
      data.aws_ssm_parameter.ed-pass.arn
    ]
  }
}

data "aws_iam_policy_document" "allow_data_buckets_access" {
  statement {
    sid = "AllowAccessToDataBuckets"
    actions = [
      "s3:PutObject*",
      "s3:GetObject*",
      "s3:ListBucket*",
    ]
    resources = concat([
      for bucket in var.data_buckets :
      "arn:aws:s3:::${bucket}"
    ],
      [
        for bucket in var.data_buckets :
        "arn:aws:s3:::${bucket}/*"
      ]
    )
  }
}

data "aws_iam_policy_document" "all_bignbit" {
  source_policy_documents = [
    data.aws_iam_policy_document.bignbit_lambda_policy.json,
    data.aws_iam_policy_document.allow_data_buckets_access.json
  ]
}

resource "aws_iam_role_policy" "bignbit_policy_attach" {
  name   = "${local.aws_resources_name}_bignbit_policy_attach"
  role   = aws_iam_role.bignbit_lambda_role.id
  policy = data.aws_iam_policy_document.all_bignbit.json
}



