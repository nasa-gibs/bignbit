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

variable "lambda_timeouts" {
  type        = map(number)
  description = "Configurable map of lambda timeouts"
  default     = {}
  validation {
    condition = alltrue([for k, v in var.lambda_timeouts : v >= 1 && v <= 900])
    error_message = "All lambda timeouts must be between 1 and 900 inclusive."
  }
}

variable "lambda_memory_sizes" {
  type        = map(number)
  description = "Configurable map of lambda memory sizes"
  default     = {}
  validation {
    condition = alltrue([for k, v in var.lambda_memory_sizes : v >= 128 && v <= 10240])
    error_message = "All lambda memory sizes must be between 128 and 10240 inclusive."
  }
}
