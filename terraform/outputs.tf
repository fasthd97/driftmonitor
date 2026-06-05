################################################################################
# outputs.tf
# ----------
#  These values are printed after terraform apply so you don't have to
# hunt through the AWS console. The most important one is
# manual_trigger_command — you'll use it to test the tool immediately
# after deploy without waiting for the EventBridge schedule to fire.
################################################################################

output "lambda_function_name" {
  description = "Name of the drift monitor Lambda function"
  value       = aws_lambda_function.drift_monitor.function_name
}

output "lambda_function_arn" {
  description = "ARN of the drift monitor Lambda function"
  value       = aws_lambda_function.drift_monitor.arn
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic that receives drift alerts"
  value       = aws_sns_topic.drift_alerts.arn
}

output "eol_cache_bucket" {
  description = "S3 bucket name where EOL data is cached"
  value       = aws_s3_bucket.eol_cache.bucket
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for Lambda run logs"
  value       = aws_cloudwatch_log_group.drift_monitor.name
}

output "manual_trigger_command" {
  description = "AWS CLI command to manually trigger a drift monitor run"
  value       = "aws lambda invoke --function-name ${aws_lambda_function.drift_monitor.function_name} --payload '{\"manual\": true}' --cli-binary-format raw-in-base64-out /tmp/drift-response.json && cat /tmp/drift-response.json"
}

output "post_deploy_note" {
  description = "Reminder about the SNS email confirmation step"
  value       = "ACTION REQUIRED: Check your email and confirm the SNS subscription from AWS. Alerts wont deliver until confirmed."
}
