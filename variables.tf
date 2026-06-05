################################################################################
# variables.tf
# ------------
# All configurable inputs for the drift monitor.
# Nothing should be hardcoded in main.tf — it all lives here.
#
# To override a default, pass -var="key=value" to terraform apply,
# or create a terraform.tfvars file in this directory.
#
# Example terraform.tfvars:
#   alert_email         = "platform-team@company.com"
#   schedule_expression = "rate(30 days)"
#   monitor_tag_key     = "team"
#   monitor_tag_value   = "platform"
################################################################################

variable "aws_region" {
  description = "AWS region to deploy the drift monitor into"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Name prefix applied to all resources created by this module. Keep it short."
  type        = string
  default     = "drift-monitor"
}

# ------------------------------------------------------------
# TAG FILTERING
# ------------------------------------------------------------
# These two variables define which CloudFormation stacks get
# monitored. A stack must have a tag matching BOTH key and value
# to be included in checks.
#
# Default: tag key "drift-monitor", tag value "enabled"
# Override example: monitor stacks tagged "team: platform"
#   monitor_tag_key   = "team"
#   monitor_tag_value = "platform"
# ------------------------------------------------------------

variable "monitor_tag_key" {
  description = "The CloudFormation stack tag key that opts a stack into drift monitoring"
  type        = string
  default     = "drift-monitor"
}

variable "monitor_tag_value" {
  description = "The CloudFormation stack tag value that opts a stack into drift monitoring"
  type        = string
  default     = "enabled"
}

# ------------------------------------------------------------
# SCHEDULE
# ------------------------------------------------------------
# Controls how often the Lambda runs automatically.
# EventBridge supports two formats:
#   rate(N unit) — e.g. rate(7 days), rate(1 hour)
#   cron(...)    — e.g. cron(0 9 1 * ? *) = 9am on the 1st of every month
#
# The EOL cache TTL below should be <= this schedule so that
# cache is always refreshed at least as often as the check runs.
# ------------------------------------------------------------

variable "schedule_expression" {
  description = "EventBridge schedule expression for how often to run the monitor"
  type        = string
  default     = "rate(7 days)"
}

# ------------------------------------------------------------
# ALERTING
# ------------------------------------------------------------

variable "alert_email" {
  description = "Email address to receive SNS drift alert notifications"
  type        = string
  # No default — this is required. User must provide it.
}

# ------------------------------------------------------------
# ANTHROPIC / AI PARSER
# ------------------------------------------------------------
# The Anthropic API key is stored in SSM Parameter Store (SecureString)
# so it's encrypted at rest and never appears in Terraform state.
#
# You must create this SSM parameter manually before running terraform apply,
# in the SAME region you are deploying to:
#
#   aws ssm put-parameter \
#     --name "/drift-monitor/anthropic-api-key" \
#     --value "sk-ant-..." \
#     --type SecureString \
#     --region us-east-1
# ------------------------------------------------------------

variable "anthropic_ssm_parameter_name" {
  description = "Full SSM Parameter Store path to the Anthropic API key SecureString"
  type        = string
  default     = "/drift-monitor/anthropic-api-key"
}

# ------------------------------------------------------------
# EOL CACHE
# ------------------------------------------------------------
# We cache scraped EOL data in S3 to avoid hitting AWS docs
# and calling the AI API on every single Lambda invocation.
#
# TTL should match or exceed your schedule interval.
# Default 168h (1 week) aligns with the default rate(7 days) schedule.
# ------------------------------------------------------------

variable "eol_cache_ttl_hours" {
  description = "How long (hours) to treat cached EOL data as valid before re-scraping"
  type        = number
  default     = 168 # 1 week
}

# ------------------------------------------------------------
# CHECK THRESHOLDS
# ------------------------------------------------------------

variable "days_until_eol_warning" {
  description = "Number of days before a runtime EOL date to start flagging it as a WARNING"
  type        = number
  default     = 90 # 3 months advance notice
}

variable "lambda_timeout_seconds" {
  description = "Lambda function timeout in seconds. Increase if you have many stacks."
  type        = number
  default     = 300 # 5 minutes
}

variable "lambda_memory_mb" {
  description = "Lambda function memory in MB"
  type        = number
  default     = 256
}

variable "log_retention_days" {
  description = "How many days to retain CloudWatch logs for this Lambda"
  type        = number
  default     = 30
}

# ------------------------------------------------------------
# BUILD SCRIPT
# ------------------------------------------------------------
# The Lambda packaging script differs by OS.
# Windows users use build.ps1, Linux/Mac users use build.sh.
#
# Windows (default):
#   terraform apply -var="alert_email=you@example.com"
#
# Linux/Mac:
#   terraform apply -var="alert_email=you@example.com" -var="build_script=../scripts/build.sh"
# ------------------------------------------------------------

variable "build_script" {
  description = "Path to the Lambda build script relative to the terraform/ directory. Use build.ps1 on Windows, build.sh on Linux/Mac."
  type        = string
  default     = "../scripts/build.ps1"
}
