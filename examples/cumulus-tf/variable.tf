variable "prefix" {
  type = string
}

variable "permissions_boundary_arn" {
  type    = string
  default = null
}

variable "edl_user" {
  type = string
  default = "urs_podaaccloud_user"
  description = "Earth Data login username ssm parameter from shared infrastructure"
}

variable "edl_pass" {
  type = string
  default = "urs_podaaccloud_pass"
  description = "Earth Data login password ssm parameter from shared infrastructure"
}

variable "bignbit_stage" {
  type = string
  description = "environment used for tagging resources"
}

variable "bignbit_cmr_environment" {
  type = string
  description = "cmr environment used for bignbit"
}

variable "bignbit_staging_bucket_region" {
  type        = string
  description = "Region for the S3 staging bucket where generated images will be saved. Defaults to us-west-2 for Harmony compatibility. This configures the aws.staging aliased provider passed into the bignbit module. See: https://github.com/nasa/harmony/blob/0f82b4f405373fe8c07eaa41a3840b0554356504/services/harmony/app/markdown/cloud-access.md?plain=1#L3"
  default     = "us-west-2"
}

variable "big_throttled_execution_limit" {
  type    = number
  default = 50
}

variable "big_throttled_message_limit" {
  type    = number
  default = 50
}

variable "big_throttled_time_limit" {
  type    = number
  default = 60
}

variable "gibs_region" {
  type        = string
  description = "Region GIBS endpoints reside in."
  default     = "us-west-2"
}

variable "gibs_account_id" {
  type        = string
  description = "Account ID for GIBS."
  default     = ""
}

variable "gibs_queue_name" {
  type        = string
  description = "Queue name for GIBS SQS queue that pobit will publish messages to."
  default     = "gitc-prod-PODAAC-IN.fifo"
}

variable "browse_image_module_count" {
  description = "set to 0 to NOT deploy browse image module"
  type        = number
  default     = 1
}

variable "app_version" {
  type        = string
}

variable "lambda_container_image_uri" {
  type        = string
}

variable "cma_version" {
  type    = string
  default = "v2.0.4"
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
