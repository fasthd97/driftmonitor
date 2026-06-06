################################################################################
# main.tf
# -------
# All AWS infrastructure for the drift monitor.
#
# Reading order (resources depend on each other in this order):
#   1. Data sources    — read existing AWS info (account ID, caller identity)
#   2. S3             — EOL data cache bucket
#   3. SNS            — alert topic + email subscription
#   4. IAM            — Lambda execution role + least-privilege policies
#   5. Build          — packages Lambda code into a zip
#   6. Lambda         — the function itself
#   7. CloudWatch     — log group with retention
#   8. EventBridge    — scheduled trigger + permission to invoke Lambda
################################################################################


################################################################################
# 1. DATA SOURCES
# ---------------
# These don't create anything. They read existing AWS state.
# aws_caller_identity gives us the account ID, which we use
# to build resource ARNs without hardcoding account numbers.
################################################################################

data "aws_caller_identity" "current" {}


################################################################################
# 2. S3 - EOL Data Cache
# ----------------------
# The Lambda scrapes AWS docs and uses AI to parse runtime EOL dates.
# That's expensive to do on every run, so we cache the result here.
# The Lambda checks the cache age and only re-scrapes when stale.
#
# Bucket name includes account ID to guarantee global uniqueness.
################################################################################

resource "aws_s3_bucket" "eol_cache" {
  bucket = "${var.project_name}-eol-cache-${data.aws_caller_identity.current.account_id}"

  tags = {
    Project = var.project_name
    Purpose = "Caches AWS runtime EOL data scraped from AWS docs"
  }
}

# Block all public access — this bucket must never be public
resource "aws_s3_bucket_public_access_block" "eol_cache" {
  bucket = aws_s3_bucket.eol_cache.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable versioning so we can recover a previous good cache
# if a scrape or parse produces bad data
resource "aws_s3_bucket_versioning" "eol_cache" {
  bucket = aws_s3_bucket.eol_cache.id

  versioning_configuration {
    status = "Enabled"
  }
}


################################################################################
# 3. SNS - Alert Topic + Email Subscription
# ------------------------------------------
# The Lambda publishes findings here. For now, email only.
# Slack/Teams will be a later phase.
#
# IMPORTANT: After terraform apply, AWS sends a confirmation email to
# var.alert_email. You MUST click "Confirm subscription" or alerts
# will not be delivered.
################################################################################

resource "aws_sns_topic" "drift_alerts" {
  name = "${var.project_name}-alerts"

  tags = {
    Project = var.project_name
    Purpose = "Drift monitoring alert notifications"
  }
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.drift_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}


################################################################################
# 4. IAM - Lambda Execution Role
# --------------------------------
# Least-privilege: one policy per service concern.
# This way you can see exactly what the Lambda can and cannot do,
# and revoke individual permissions without touching others.
#
# Policies attached:
#   - cloudwatch_logs     : write Lambda run logs
#   - cloudformation_read : list and describe stacks and their resources
#   - lambda_read         : get function configuration (runtime, etc.)
#   - s3_cache            : read/write our EOL cache bucket only
#   - sns_publish         : publish to our alerts topic only
#   - ssm_read            : read the Anthropic API key SecureString
################################################################################

# Trust policy: allows the Lambda SERVICE to assume this role.
# Without this, no Lambda function could use this role.
data "aws_iam_policy_document" "lambda_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "drift_monitor" {
  name               = "${var.project_name}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json

  tags = {
    Project = var.project_name
  }
}

# --- CloudWatch Logs ---
# Lambda needs this to write execution logs.
# Scoped to just this function's log group — not all log groups.
data "aws_iam_policy_document" "cloudwatch_logs" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_name}:*"
    ]
  }
}

resource "aws_iam_policy" "cloudwatch_logs" {
  name   = "${var.project_name}-cloudwatch-logs"
  policy = data.aws_iam_policy_document.cloudwatch_logs.json
}

resource "aws_iam_role_policy_attachment" "cloudwatch_logs" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.cloudwatch_logs.arn
}

# --- CloudFormation Read ---
# The Lambda needs to list all stacks and read their tags + resources.
# Unfortunately CloudFormation doesn't support resource-level
# restrictions on List/Describe — the resource must be "*".
data "aws_iam_policy_document" "cloudformation_read" {
  statement {
    effect = "Allow"
    actions = [
      "cloudformation:ListStacks",
      "cloudformation:DescribeStacks",
      "cloudformation:ListStackResources",
    ]
    resources = ["*"] # CF API limitation — cannot be scoped further
  }
}

resource "aws_iam_policy" "cloudformation_read" {
  name   = "${var.project_name}-cloudformation-read"
  policy = data.aws_iam_policy_document.cloudformation_read.json
}

resource "aws_iam_role_policy_attachment" "cloudformation_read" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.cloudformation_read.arn
}

# --- Lambda Read ---
# To check the ACTUAL deployed runtime (not just the CF template),
# we call the Lambda API directly on discovered functions.
data "aws_iam_policy_document" "lambda_read" {
  statement {
    effect = "Allow"
    actions = [
      "lambda:GetFunctionConfiguration",
    ]
    resources = ["*"] # We don't know function ARNs in advance
  }
}

resource "aws_iam_policy" "lambda_read" {
  name   = "${var.project_name}-lambda-read"
  policy = data.aws_iam_policy_document.lambda_read.json
}

resource "aws_iam_role_policy_attachment" "lambda_read" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.lambda_read.arn
}

# --- S3 Cache ---
# Scoped to only our EOL cache bucket — not all S3 buckets.
# s3:ListBucket is required on the bucket itself (not objects) so that
# S3 returns a proper 404 (NoSuchKey) instead of an AccessDenied error
# when checking for a cache miss.
data "aws_iam_policy_document" "s3_cache" {
  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:HeadObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.eol_cache.arn,
      "${aws_s3_bucket.eol_cache.arn}/*",
    ]
  }
}

resource "aws_iam_policy" "s3_cache" {
  name   = "${var.project_name}-s3-cache"
  policy = data.aws_iam_policy_document.s3_cache.json
}

resource "aws_iam_role_policy_attachment" "s3_cache" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.s3_cache.arn
}

# --- SNS Publish ---
# Publish-only. The Lambda cannot create, delete, or list topics.
data "aws_iam_policy_document" "sns_publish" {
  statement {
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.drift_alerts.arn]
  }
}

resource "aws_iam_policy" "sns_publish" {
  name   = "${var.project_name}-sns-publish"
  policy = data.aws_iam_policy_document.sns_publish.json
}

resource "aws_iam_role_policy_attachment" "sns_publish" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.sns_publish.arn
}

# --- SSM Read ---
# Read-only access to exactly one parameter: the Anthropic API key.
# WithDecryption=true (in the Python code) requires kms:Decrypt too.
# We use the AWS-managed key (aws/ssm), so resources = "*" for KMS is acceptable.
# If you use a customer-managed CMK, tighten this to that key's ARN.
data "aws_iam_policy_document" "ssm_read" {
  statement {
    effect  = "Allow"
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.anthropic_ssm_parameter_name}"
    ]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["*"] # AWS-managed key; tighten if using a custom CMK
  }
}

resource "aws_iam_policy" "ssm_read" {
  name   = "${var.project_name}-ssm-read"
  policy = data.aws_iam_policy_document.ssm_read.json
}

resource "aws_iam_role_policy_attachment" "ssm_read" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.ssm_read.arn
}


################################################################################
# 5. LAMBDA BUILD
# ----------------
# We need to package our Python source + dependencies into a zip file
# that AWS Lambda can deploy. This can't happen inside Terraform natively,
# so we use null_resource + local-exec to run a build script.
#
# Cross-platform support:
#   Windows (default): scripts/build.ps1
#   Linux/Mac:         scripts/build.sh
#
# Set via variable at deploy time:
#   terraform apply -var="build_script=../scripts/build.sh"
#
# How the trigger works:
#   - We hash all .py files in the lambda/ directory
#   - If any file changes, the hash changes → Terraform re-runs the build
#   - If nothing changed, Terraform skips the build step (fast)
################################################################################

locals {
  # Collect all Python source files so we can hash them as a group.
  # sort() ensures consistent ordering across OS/filesystem differences.
  lambda_source_files = sort(tolist(fileset("${path.module}/../lambda", "**/*.py")))

  # Combine all file hashes into one value.
  # If ANY .py file changes, this hash changes, triggering a rebuild.
  lambda_source_hash = sha256(join("", [
    for f in local.lambda_source_files :
    filesha256("${path.module}/../lambda/${f}")
  ]))

  # Determine interpreter and command based on which build script is selected.
  # PowerShell for .ps1, bash for .sh.
  is_windows   = can(regex("\\.ps1$", var.build_script))
  interpreter  = local.is_windows ? ["powershell", "-Command"] : ["bash", "-c"]
  build_command = local.is_windows ? "powershell -ExecutionPolicy Bypass -File ${abspath(path.module)}/${var.build_script}" : "bash ${abspath(path.module)}/${var.build_script}"
}

resource "null_resource" "build_lambda" {
  # Re-run the build script when source or dependencies change
  triggers = {
    source_hash       = local.lambda_source_hash
    requirements_hash = filemd5("${path.module}/../lambda/requirements.txt")
    build_script      = var.build_script
  }

  provisioner "local-exec" {
    # Runs the appropriate build script for the current OS.
    # Windows: build.ps1 via PowerShell
    # Linux/Mac: build.sh via bash
    command     = local.build_command
    interpreter = local.interpreter
  }
}

# Zip the built package directory for deployment.
# depends_on ensures the build always runs before we try to zip.
# output_base64sha256 is used by aws_lambda_function to detect code changes
# and trigger a function update.
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../dist/package"
  output_path = "${path.module}/../dist/lambda.zip"

  depends_on = [null_resource.build_lambda]
}


################################################################################
# 6. LAMBDA FUNCTION
# -------------------
# The actual Lambda function. Non-secret config is passed as environment
# variables. Secrets (Anthropic API key) stay in SSM and are fetched at
# runtime — they never appear in the Lambda config or Terraform state.
################################################################################

resource "aws_lambda_function" "drift_monitor" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = var.project_name
  role             = aws_iam_role.drift_monitor.arn
  handler          = "handler.lambda_handler" # file: handler.py, function: lambda_handler
  runtime          = "python3.12"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  # Drift checks can be slow with many stacks — give it room
  timeout     = var.lambda_timeout_seconds
  memory_size = var.lambda_memory_mb

  environment {
    variables = {
      # Non-secret runtime config — safe to store as plain env vars
      SNS_TOPIC_ARN           = aws_sns_topic.drift_alerts.arn
      EOL_CACHE_BUCKET        = aws_s3_bucket.eol_cache.bucket
      MONITOR_TAG_KEY         = var.monitor_tag_key
      MONITOR_TAG_VALUE       = var.monitor_tag_value
      DAYS_UNTIL_EOL_WARNING  = tostring(var.days_until_eol_warning)
      EOL_CACHE_TTL_HOURS     = tostring(var.eol_cache_ttl_hours)

      # SSM parameter NAME only — the key itself is fetched at runtime
      ANTHROPIC_SSM_PARAMETER = var.anthropic_ssm_parameter_name
    }
  }

  tags = {
    Project = var.project_name
  }
}


################################################################################
# 7. CLOUDWATCH LOG GROUP
# ------------------------
# Explicitly creating the log group (rather than letting Lambda auto-create it)
# gives us control over the retention period. Without this, logs are kept
# forever and can accumulate significant cost over time.
################################################################################

resource "aws_cloudwatch_log_group" "drift_monitor" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = var.log_retention_days

  tags = {
    Project = var.project_name
  }
}


################################################################################
# 8. EVENTBRIDGE - Scheduled Trigger
# ------------------------------------
# EventBridge (formerly CloudWatch Events) triggers the Lambda on a schedule.
# Two resources are needed:
#   - aws_cloudwatch_event_rule   : defines the schedule
#   - aws_cloudwatch_event_target : connects the rule to the Lambda
#
# Plus a Lambda permission that allows EventBridge to invoke the function.
# Without that permission, EventBridge can create the target but AWS will
# silently refuse to invoke the Lambda.
################################################################################

resource "aws_cloudwatch_event_rule" "drift_monitor_schedule" {
  name                = "${var.project_name}-schedule"
  description         = "Triggers the drift monitor Lambda on a schedule"
  schedule_expression = var.schedule_expression

  tags = {
    Project = var.project_name
  }
}

resource "aws_cloudwatch_event_target" "drift_monitor" {
  rule      = aws_cloudwatch_event_rule.drift_monitor_schedule.name
  target_id = "DriftMonitorLambda"
  arn       = aws_lambda_function.drift_monitor.arn
}

# This is the permission that actually allows EventBridge to call the Lambda.
# Without it, the rule fires but the Lambda is never invoked.
resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.drift_monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.drift_monitor_schedule.arn
}
################################################################################
# 9. CLOUDWATCH ALARMS - Self Monitoring
# ---------------------------------------
# These alarms watch the drift-monitor Lambda itself so we know if the
# tool breaks or stops running. Without this, a silently failing Lambda
# would mean stacks go unmonitored with no indication anything is wrong.
#
# All alarms publish to the same SNS topic as drift findings so alerts
# land in the same inbox.
#
# Three alarms:
#   - Errors    : Lambda threw an uncaught exception
#   - Throttles : AWS rate-limited the Lambda (couldn't run)
#   - Invocations : Lambda hasn't run in 8 days (dead man's switch)
################################################################################

# --- Alarm 1: Lambda Errors ---
# Fires if the Lambda throws any uncaught exception.
# Even one error in an hour is worth knowing about since this
# runs on a weekly schedule — every run matters.
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project_name}-errors"
  alarm_description   = "Drift monitor Lambda threw an uncaught exception. Check CloudWatch logs."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.drift_monitor.function_name
  }

  statistic           = "Sum"
  period              = 3600       # Evaluate over 1 hour windows
  evaluation_periods  = 1          # Alarm after 1 consecutive breach
  threshold           = 1          # Breach if >= 1 error
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # If no data exists (Lambda never ran in this period), treat as OK.
  # We have a separate alarm for that case below.
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.drift_alerts.arn]
  ok_actions    = [aws_sns_topic.drift_alerts.arn]

  tags = {
    Project = var.project_name
    Purpose = "Alert if the drift monitor Lambda errors"
  }
}

# --- Alarm 2: Lambda Throttles ---
# Fires if AWS rate-limited the Lambda — means it tried to run
# but AWS blocked it due to concurrency limits.
# Throttles mean your stacks were not checked even though the
# schedule fired.
resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${var.project_name}-throttles"
  alarm_description   = "Drift monitor Lambda was throttled by AWS. Stacks may not have been checked."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions = {
    FunctionName = aws_lambda_function.drift_monitor.function_name
  }

  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.drift_alerts.arn]
  ok_actions    = [aws_sns_topic.drift_alerts.arn]

  tags = {
    Project = var.project_name
    Purpose = "Alert if the drift monitor Lambda is throttled"
  }
}

# --- Alarm 3: Lambda Not Invoked (Dead Man's Switch) ---
# Fires if the Lambda has not run at all in 8 days.
# This catches silent failures where EventBridge stops firing
# or the Lambda is deleted/disabled without anyone noticing.
#
# treat_missing_data = "breaching" is the key here — if there
# are NO invocations in the 8 day window, CloudWatch has no data
# points. Setting breaching means no data = alarm triggers.
# Without this, no data would just show as INSUFFICIENT_DATA
# and the alarm would never fire.
resource "aws_cloudwatch_metric_alarm" "lambda_not_invoked" {
  alarm_name          = "${var.project_name}-not-invoked"
  alarm_description   = "Drift monitor Lambda has not run in 7 days. EventBridge may have stopped firing."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions = {
    FunctionName = aws_lambda_function.drift_monitor.function_name
  }

  statistic           = "Sum"
  period              = 86400     # 1 days in seconds (8 * 24 * 60 * 60)
  evaluation_periods  = 7
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Critical setting: no data means the Lambda never ran = breach
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.drift_alerts.arn]

  tags = {
    Project = var.project_name
    Purpose = "Alert if the drift monitor Lambda stops running entirely"
  }
}