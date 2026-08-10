################################################################################
# versions.tf
# -----------
# Pins all provider versions so that a future `terraform init` doesn't
# silently pull a newer, potentially breaking provider version.
#
# Rule of thumb: pin major + minor (~> 5.0 allows 5.x but not 6.0).
# Run `terraform init -upgrade` intentionally when you want to bump providers.
################################################################################

terraform {
  # Minimum Terraform CLI version required to use this config
  required_version = ">= 1.5.0"

  required_providers {
    # AWS provider — manages all AWS resources
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.58"
    }

    # Archive provider — zips the Lambda source code for deployment
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }

    # Null provider — used to run the Lambda build script as a local-exec step
    null = {
      source  = "hashicorp/null"
      version = "~> 3.3"
    }
  }
}

# Configure the AWS provider with the region from variables
# Credentials come from your local AWS CLI config or environment variables
provider "aws" {
  region = var.aws_region
}
