# Create a bucket to store bignbit results in unless one is provided as a variable
locals {
  create_bucket       = var.bignbit_staging_bucket == ""
  staging_bucket_name = local.create_bucket ? aws_s3_bucket.bignbit_staging_bucket[0].id : var.bignbit_staging_bucket
}

resource "aws_s3_bucket" "bignbit_staging_bucket" {
  provider      = aws.staging
  count         = local.create_bucket ? 1 : 0
  bucket        = "${local.aws_resources_name}-staging"
  force_destroy = var.force_destroy_staging_bucket

  lifecycle {
    ignore_changes = [
      lifecycle_rule
    ]
  }
}

resource "aws_s3_bucket_ownership_controls" "disable_acls" {
  provider = aws.staging
  count    = local.create_bucket ? 1 : 0
  bucket   = aws_s3_bucket.bignbit_staging_bucket[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "enable_bucket_encryption" {
  provider = aws.staging
  count    = local.create_bucket ? 1 : 0
  bucket   = aws_s3_bucket.bignbit_staging_bucket[0].id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "expire_objects_30_days" {
  provider = aws.staging
  count    = local.create_bucket ? 1 : 0
  bucket   = aws_s3_bucket.bignbit_staging_bucket[0].id

  rule {
    id     = "ExpireHarmonyObjectsAfter30Days"
    status = "Enabled"
    expiration {
      days = 30
    }
    filter {
      prefix = var.harmony_staging_path
    }
  }

  rule {
    id     = "ExpireOperaHLSObjectsAfter30Days"
    status = "Enabled"
    expiration {
      days = 30
    }
    filter {
      prefix = "opera_hls_processing/"
    }
  }
}

resource "aws_s3_bucket_policy" "bignbit_staging_bucket_policy" {
  provider = aws.staging
  count    = local.create_bucket ? 1 : 0
  bucket   = aws_s3_bucket.bignbit_staging_bucket[0].id
  policy   = data.aws_iam_policy_document.bignbit_staging_bucket_policy[0].json
}

data "aws_iam_policy_document" "allow_harmony_write" {
  count = local.create_bucket ? 1 : 0
  statement {
    sid    = "write permission"
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    resources = [
      "${aws_s3_bucket.bignbit_staging_bucket[0].arn}/${var.harmony_staging_path}/*"
    ]
    principals {
      identifiers = ["arn:aws:iam::549360732797:root", "arn:aws:iam::625642860590:root"]
      type = "AWS"
    }
  }
}

data "aws_iam_policy_document" "allow_harmony_location" {
  count = local.create_bucket ? 1 : 0
  statement {
    sid    = "get bucket location permission"
    effect = "Allow"
    actions = [
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.bignbit_staging_bucket[0].arn
    ]
    principals {
      identifiers = ["arn:aws:iam::549360732797:root", "arn:aws:iam::625642860590:root"]
      type = "AWS"
    }
  }
}

data "aws_iam_policy_document" "allow_gibs_getobject" {
  count = local.create_bucket ? 1 : 0
  statement {
    sid    = "gibs get object"
    effect = "Allow"
    actions = [
      "s3:GetObject*",
    ]
    resources = [
      "${aws_s3_bucket.bignbit_staging_bucket[0].arn}/${var.harmony_staging_path}/*",
      "${aws_s3_bucket.bignbit_staging_bucket[0].arn}/opera_hls_processing/*"
    ]
    principals {
      identifiers = ["arn:aws:iam::${var.gibs_account_id}:root"]
      type = "AWS"
    }
  }
}

data "aws_iam_policy_document" "bignbit_staging_bucket_policy" {
  count = local.create_bucket ? 1 : 0
  source_policy_documents = [
    data.aws_iam_policy_document.allow_harmony_write[0].json,
    data.aws_iam_policy_document.allow_harmony_location[0].json,
    data.aws_iam_policy_document.allow_gibs_getobject[0].json
  ]
}