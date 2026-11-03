variable "stage" {
  type = string
  description = "Environment used for resource tagging (dev, int, ops, etc...)"
}

variable "cmr_environment" {
  type = string
  description = "Environment used when querying CMR during GIBS response handling. Defaults to var.stage if not provided"
  default = ""

  validation {
    condition     = contains(["UAT", "OPS"], upper(var.cmr_environment)) || var.cmr_environment == ""
    error_message = "Valid values for var: cmr_environment are (UAT, OPS)."
  }

}

variable "prefix" {
  type = string
  description = "Prefix used for resource naming (project name, env name, etc...)"
}

variable "data_buckets" {
  type = set(string)
  description = "Buckets where data to be imaged is stored (cumulus-public, cumulus-protected, etc...)"
  default = []
}

variable "config_bucket" {
  type = string
  description = "Bucket where dataset configuration is stored"
}

variable "config_dir" {
  type = string
  description = "Path relative to config_bucket where dataset configuration is stored"
  default = "big-config"
}

variable "bignbit_audit_bucket" {
  type = string
  description = "S3 bucket where messages exchanged with GIBS will be saved. Typically the cumulus internal bucket"
}

variable "bignbit_audit_path" {
  type = string
  description = "Path relative to bignbit_audit_bucket where messages exchanged with GIBS will be saved."
  default = "bignbit-cnm-output"
}

variable "bignbit_staging_bucket" {
  type = string
  description = "S3 bucket where generated images will be saved. Leave blank to use bucket managed by this module."
  default = ""
}

variable "bignbit_staging_bucket_region_override" {
  type = bool
  description = "Bypass the S3 bucket region check for us-west-2. Warning: This will break Harmony compatibility."
  default = false
}

variable "harmony_staging_path" {
  type = string
  description = "Path relative to bignbit_staging_bucket where harmony results will be saved."
  default = "bignbit-harmony-output"
}

variable "gibs_region" {
  type = string
  description = "Region where GIBS resources are deployed"
}

variable "gibs_queue_name" {
  type = string
  description = "Name of the GIBS SQS queue where outgoing CNM messages will be sent"
}

variable "gibs_account_id" {
  type = string
  description = "AWS account ID for GIBS"
}

variable "create_gibs_mock_queue" {
  type        = bool
  default     = false
  description = "When true, creates an internal SQS FIFO queue to stand in for the external GIBS queue. Use in sandbox/sit environments."
}

variable "edl_user_ssm" {
  type = string
  description = "Name of SSM parameter containing EDL username for querying CMR"
}

variable "edl_pass_ssm" {
  type = string
  description = "Name of SSM parameter containing EDL password for querying CMR"
}

variable "permissions_boundary_arn" {
  type = string
  description = "Permissions boundary ARN to apply to the roles created by this module. If not provided, no permissions boundary will be applied."
}

variable "security_group_ids" {
  type = list(string)
}

variable "subnet_ids" {
  type = list(string)
}

variable "app_name" {
  default = "bignbit"
}

variable "default_tags" {
  type = map(string)
  default = {}
}

variable "lambda_container_image_uri" {
  type = string
  default = ""
}

variable "force_delete_ecr" {
  type        = bool
  description = "Force delete the ECR repository and all its images on destroy. Set to true when migrating resource names to avoid manual cleanup."
  default     = false
}

variable "force_destroy_staging_bucket" {
  type        = bool
  description = "Force destroy the staging S3 bucket and all its contents on destroy. Set to true when migrating resource names to avoid manual cleanup."
  default     = false
}

variable "harmony_job_status_interval_seconds" {
    type = number
    description = "Interval in seconds for checking Harmony job status"
    default = 20
}

variable "harmony_job_status_max_attempts" {
    type = number
    description = "Maximum number of attempts to check Harmony job status"
    default = 15
}

variable "harmony_job_status_backoff_rate" {
    type = number
    description = "Backoff rate for Harmony job status checks"
    default = 1.0
}

variable "harmony_job_status_max_delay_seconds" {
    type = number
    description = "Maximum delay in seconds for Harmony job status checks"
    default = 20
}
