
resource "aws_sqs_queue" "gibs_mock_queue" {
  count                       = var.create_gibs_mock_queue ? 1 : 0
  name                        = "${local.aws_resources_name}-gibs-mock-queue.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
}

data "aws_iam_policy_document" "gibs_response_topic_policy" {
  statement {
    sid       = "${local.aws_resources_name}-grant-gitc-publish-sns"
    effect    = "Allow"
    principals {
      identifiers = compact([
        var.create_gibs_mock_queue ? "" : var.gibs_account_id,
        data.aws_caller_identity.current.account_id
      ])
      type        = "AWS"
    }
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.gibs_response_topic.arn]
  }
}

resource "aws_sns_topic_policy" "default" {
  arn = aws_sns_topic.gibs_response_topic.arn
  policy = data.aws_iam_policy_document.gibs_response_topic_policy.json
}

resource "aws_sns_topic" "gibs_response_topic" {
  name   = "svc-${local.sns_topic_name}-gibs-response-topic"
  lifecycle {
    # GIBS publishes to this topic, so we want to avoid destroying it unless coordinating the change with GIBS
    prevent_destroy = true
  }
}

resource "aws_sqs_queue" "gibs_response_queue" {
  name = "${local.aws_resources_name}-gibs-response-queue"
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.gibs_response_deadletter.arn
    maxReceiveCount     = 4
  })
  visibility_timeout_seconds = 45
}

resource "aws_sqs_queue" "gibs_response_deadletter" {
  name = "${local.aws_resources_name}-gibs-response-dlq"
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue",
    # Cannot use reference to aws_sqs_queue.gibs_response_queue.arn because it causes a cycle https://github.com/hashicorp/terraform-provider-aws/issues/22577
    sourceQueueArns   = ["arn:aws:sqs:${data.aws_region.current.name}:${local.account_id}:${local.aws_resources_name}-gibs-response-queue"]
  })
}

data "aws_iam_policy_document" "gibs_response_queue_policy" {
  statement {
    sid       = "${local.aws_resources_name}-grant-topic-send-sqs"
    effect    = "Allow"
    principals {
      identifiers = [
        data.aws_caller_identity.current.account_id
      ]
      type        = "AWS"
    }
    principals {
      identifiers = ["sns.amazonaws.com"]
      type        = "Service"
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.gibs_response_queue.arn]
    condition {
      test     = "ArnEquals"
      values   = [aws_sns_topic.gibs_response_topic.arn]
      variable = "aws:SourceArn"
    }
  }
}

resource "aws_sqs_queue_policy" "gibs_response_queue_policy" {
  queue_url = aws_sqs_queue.gibs_response_queue.id
  policy    = data.aws_iam_policy_document.gibs_response_queue_policy.json
}

resource "aws_sns_topic_subscription" "gibs_topic_subscription" {
  topic_arn            = aws_sns_topic.gibs_response_topic.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.gibs_response_queue.arn
  raw_message_delivery = true
}

data "aws_iam_policy_document" "gibs_response_role_policy" {
  statement {
    sid       = "${replace(title(replace(local.aws_resources_name, "-", " ")), " ", "")}GrantReadGitcResponse"
    effect    = "Allow"
    actions   = ["sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:PurgeQueue",
      "sqs:ReceiveMessage"
    ]
    resources = [aws_sqs_queue.gibs_response_queue.arn]
  }
  statement {
    sid       = "${replace(title(replace(local.aws_resources_name, "-", " ")), " ", "")}GrantReadGitcResponse2"
    effect    = "Allow"
    actions   = ["sqs:ListQueues"]
    resources = ["arn:aws:sqs:*:${local.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "allow_lambda_role_to_read_sqs_messages" {
  name_prefix   = local.aws_resources_name
  role   = aws_iam_role.bignbit_lambda_role.id
  policy = data.aws_iam_policy_document.gibs_response_role_policy.json
}

resource "aws_lambda_event_source_mapping" "gibs_response_event_trigger" {
  event_source_arn = aws_sqs_queue.gibs_response_queue.arn
  function_name    = aws_lambda_function.handle_gitc_response.arn

}

data "aws_iam_policy_document" "gibs_request_queue_policy" {
  statement {
    sid       = "${replace(title(replace(local.aws_resources_name, "-", " ")), " ", "")}GrantSendToGitc"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = var.create_gibs_mock_queue ? [aws_sqs_queue.gibs_mock_queue[0].arn] : ["arn:aws:sqs:*:${var.gibs_account_id}:${var.gibs_queue_name}"]
  }

  statement {
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:DeleteMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
    ]
    resources = var.create_gibs_mock_queue ? [
      "arn:aws:sqs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"
    ] : [
      "arn:aws:sqs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*",
      "arn:aws:sqs:${var.gibs_region}:${var.gibs_account_id}:${var.gibs_queue_name}"
    ]
  }
}

resource "aws_iam_role_policy" "allow_lambda_role_to_send_to_gitc" {
  name_prefix   = local.aws_resources_name
  role   = aws_iam_role.bignbit_lambda_role.id
  policy = data.aws_iam_policy_document.gibs_request_queue_policy.json
}
