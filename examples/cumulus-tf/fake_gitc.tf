resource "aws_sqs_queue" "gitc_input_queue" {
  count = local.use_mock_gitc ? 1 : 0

  name = "${local.ec2_resources_name}-fake-gitc-IN.fifo"
  fifo_queue = true
  content_based_deduplication = true
  tags = local.default_tags
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.gitc_input_deadletter[0].arn
    maxReceiveCount     = 4
  })
}

resource "aws_sqs_queue" "gitc_input_deadletter" {
  count = local.use_mock_gitc ? 1 : 0

  name = "${local.ec2_resources_name}-fake-gitc-IN-dlq.fifo"
  fifo_queue = true
  content_based_deduplication = true
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue",
    sourceQueueArns   = ["arn:aws:sqs:${data.aws_region.current.name}:${local.account_id}:${local.ec2_resources_name}-fake-gitc-IN.fifo"]
  })
}

resource "aws_lambda_function" "fakeGitcProcessing" {
  count = local.use_mock_gitc ? 1 : 0

  filename         = "./bin/fake_gitc.py.zip"
  function_name    = "${local.ec2_resources_name}-fake_gitc"
  source_code_hash = filebase64sha256("./bin/fake_gitc.py.zip")
  handler          = "fake_gitc.handler"
  role             = aws_iam_role.iam_execution.arn
  runtime          = "python3.10"
  timeout          = 5
  memory_size      = 128

  vpc_config {
    subnet_ids         = []
    security_group_ids = []
  }

  tags = local.default_tags
}


resource "aws_lambda_event_source_mapping" "gibs_response_event_trigger" {
  count = local.use_mock_gitc ? 1 : 0

  event_source_arn = aws_sqs_queue.gitc_input_queue[0].arn
  function_name    = aws_lambda_function.fakeGitcProcessing[0].arn
}

data "aws_iam_policy_document" "gitc_input_role_policy" {
  count = local.use_mock_gitc ? 1 : 0

  statement {
    sid       = "${replace(title(replace(local.ec2_resources_name, "-", " ")), " ", "")}GrantReadGitcInput"
    effect    = "Allow"
    actions   = ["sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:PurgeQueue",
      "sqs:ReceiveMessage"
    ]
    resources = [aws_sqs_queue.gitc_input_queue[0].arn]
  }
  statement {
    sid       = "${replace(title(replace(local.ec2_resources_name, "-", " ")), " ", "")}GrantReadGitcInput2"
    effect    = "Allow"
    actions   = ["sqs:ListQueues"]
    resources = ["arn:aws:sqs:*:${local.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "allow_lambda_role_to_read_sqs_messages" {
  count = local.use_mock_gitc ? 1 : 0

  name_prefix   = local.ec2_resources_name
  role   = aws_iam_role.iam_execution.name
  policy = data.aws_iam_policy_document.gitc_input_role_policy[0].json
}
