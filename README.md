# drift-monitor

A serverless AWS tool that monitors CloudFormation stacks for dependency drift —
deprecated runtimes, approaching EOL dates, and other dependency risks that
build up silently and only surface when you try to update a stack.

## The problem it solves

CloudFormation stacks that aren't updated regularly accumulate dependency
drift. Lambda runtimes get deprecated. When you finally need to update the
stack, you're not just applying your change — you're untangling months of
accumulated breakage, often under pressure. This tool catches it early by
running on a schedule and alerting you before things break.

## How it works

```
EventBridge (schedule)
       ↓
   Lambda
       ↓
1. Scrapes AWS docs for runtime EOL dates
2. Claude AI parses the HTML into structured JSON
3. Caches result in S3 (refreshed on your schedule)
       ↓
4. Lists CloudFormation stacks tagged [drift-monitor: enabled]
5. Gets ACTUAL deployed runtime for each Lambda function via Lambda API
6. Compares against EOL data
       ↓
7. CRITICAL/WARNING findings → SNS (email) + CloudWatch logs
8. Clean run → CloudWatch logs only (no email noise)
```

## Architecture

| Component | Resource | Purpose |
|---|---|---|
| Trigger | EventBridge rule | Scheduled + manual invocation |
| Compute | Lambda (Python 3.12) | All check logic |
| Alerting | SNS topic | Email notifications |
| Cache | S3 bucket | EOL data (scrape results) |
| Secrets | SSM Parameter Store | Anthropic API key (SecureString) |
| Logs | CloudWatch log group | Full run history, 30-day retention |

## Prerequisites

- AWS CLI configured with appropriate permissions
- Terraform >= 1.5.0
- Python 3.12+ and pip
- An Anthropic API key (https://console.anthropic.com)
- Windows: PowerShell — build script is `scripts/build.ps1`
- Linux/Mac: bash — build script is `scripts/build.sh`

---

## Setup

### Step 1: Store your Anthropic API key in SSM

The Lambda fetches the key at runtime from SSM in its own region.
**The SSM parameter must be in the same region you deploy to** — if they
differ you will get ParameterNotFound errors at runtime.

```bash
# Linux/Mac
aws ssm put-parameter \
  --name "/drift-monitor/anthropic-api-key" \
  --value "sk-ant-..." \
  --type SecureString \
  --description "Anthropic API key for drift-monitor" \
  --region us-east-1
```

```powershell
# Windows
aws ssm put-parameter `
  --name "/drift-monitor/anthropic-api-key" `
  --value "sk-ant-..." `
  --type SecureString `
  --description "Anthropic API key for drift-monitor" `
  --region us-east-1
```

Verify it saved correctly:
```bash
aws ssm get-parameter \
  --name "/drift-monitor/anthropic-api-key" \
  --with-decryption \
  --query "Parameter.Value" \
  --output text \
  --region us-east-1
```

If you already have the key stored in a different region, copy it across:
```powershell
# Windows — copies from us-west-2 to us-east-1
aws ssm put-parameter `
  --name "/drift-monitor/anthropic-api-key" `
  --value (aws ssm get-parameter --name "/drift-monitor/anthropic-api-key" --with-decryption --query "Parameter.Value" --output text --region us-west-2) `
  --type SecureString `
  --region us-east-1
```

### Step 2: Deploy a sample stack to monitor (optional but recommended for testing)

A sample CloudFormation template is included at `private-bucket.yaml`.
It deploys a private S3 bucket with signed URL support and tags it
`drift-monitor: enabled` so the tool picks it up immediately.

Deploy it to the same region as the drift-monitor:

```bash
# Linux/Mac
aws cloudformation create-stack \
  --stack-name my-test-stack \
  --template-body file://private-bucket.yaml \
  --parameters ParameterKey=BucketPrefix,ParameterValue=my-test-bucket \
  --tags Key=drift-monitor,Value=enabled \
  --region us-east-1

aws cloudformation wait stack-create-complete \
  --stack-name my-test-stack \
  --region us-east-1
```

```powershell
# Windows
aws cloudformation create-stack `
  --stack-name my-test-stack `
  --template-body file://private-bucket.yaml `
  --parameters ParameterKey=BucketPrefix,ParameterValue=my-test-bucket `
  --tags Key=drift-monitor,Value=enabled `
  --region us-east-1

aws cloudformation wait stack-create-complete `
  --stack-name my-test-stack `
  --region us-east-1
```

To monitor an existing stack instead, add the tag to it:
```bash
aws cloudformation update-stack \
  --stack-name YOUR-STACK-NAME \
  --use-previous-template \
  --tags Key=drift-monitor,Value=enabled \
  --region us-east-1
```

### Step 3: Deploy the drift-monitor

**Windows:**
```powershell
cd terraform
terraform init
terraform plan -var="alert_email=you@example.com"
terraform apply -var="alert_email=you@example.com"
```

**Linux/Mac:**
```bash
cd terraform
terraform init
terraform plan \
  -var="alert_email=you@example.com" \
  -var="build_script=../scripts/build.sh"

terraform apply \
  -var="alert_email=you@example.com" \
  -var="build_script=../scripts/build.sh"
```

Terraform automatically runs the build script to package the Lambda.
You do not need to run it manually.

### Step 4: Confirm SNS email subscription

AWS sends a confirmation email to the address you provided immediately
after deploy. **Click "Confirm subscription"** — alerts will not deliver
until you do.

### Step 5: Test manually

Always pass `--region` explicitly since your AWS CLI default region may
differ from where the drift-monitor is deployed.

```bash
# Linux/Mac
aws lambda invoke \
  --function-name drift-monitor \
  --payload '{"manual": true}' \
  --cli-binary-format raw-in-base64-out \
  response.json \
  --region us-east-1

cat response.json
```

```powershell
# Windows
aws lambda invoke `
  --function-name drift-monitor `
  --payload '{\"manual\": true}' `
  --cli-binary-format raw-in-base64-out `
  response.json `
  --region us-east-1

cat response.json
```

A successful run with tagged stacks returns:
```json
{"statusCode": 200, "body": "{\"run_type\": \"MANUAL\", \"stacks_checked\": 1, \"findings\": 0}"}
```

### Step 6: Check CloudWatch logs

Get the latest log stream name:
```bash
aws logs describe-log-streams \
  --log-group-name /aws/lambda/drift-monitor \
  --region us-east-1 \
  --order-by LastEventTime \
  --descending \
  --output text \
  --query "logStreams[0].logStreamName"
```

Read the logs — use single quotes around the stream name to prevent
your shell from expanding `$LATEST` as a variable:
```bash
# Linux/Mac
aws logs get-log-events \
  --log-group-name /aws/lambda/drift-monitor \
  --log-stream-name '2026/06/05/[$LATEST]abc123...' \
  --region us-east-1 \
  --query "events[].message" \
  --output text
```

```powershell
# Windows — single quotes are essential here
aws logs get-log-events `
  --log-group-name /aws/lambda/drift-monitor `
  --log-stream-name '2026/06/05/[$LATEST]abc123...' `
  --region us-east-1 `
  --query "events[].message" `
  --output text
```

---

## Configuration

All settings are in `terraform/variables.tf`. Pass overrides with `-var`
or create a `terraform.tfvars` file in the `terraform/` directory.

| Variable | Default | Description |
|---|---|---|
| `aws_region` | `us-east-1` | AWS region to deploy into |
| `monitor_tag_key` | `drift-monitor` | Tag key that opts a stack in |
| `monitor_tag_value` | `enabled` | Tag value that opts a stack in |
| `schedule_expression` | `rate(7 days)` | How often to run |
| `alert_email` | *(required)* | Email for SNS alerts |
| `anthropic_ssm_parameter_name` | `/drift-monitor/anthropic-api-key` | SSM path to API key |
| `eol_cache_ttl_hours` | `168` (1 week) | Max age of cached EOL data |
| `days_until_eol_warning` | `90` | Days before EOL to start flagging |
| `log_retention_days` | `30` | CloudWatch log retention |
| `lambda_timeout_seconds` | `300` | Lambda timeout |
| `build_script` | `../scripts/build.ps1` | Build script — change to `../scripts/build.sh` on Linux/Mac |

Example `terraform.tfvars` for a team using Linux:
```hcl
alert_email            = "platform-team@company.com"
schedule_expression    = "rate(30 days)"
monitor_tag_key        = "team"
monitor_tag_value      = "platform"
days_until_eol_warning = 60
build_script           = "../scripts/build.sh"
```

---

## Important: region consistency

Everything must be in the same AWS region:
- The drift-monitor Lambda (set by `aws_region` in `variables.tf`)
- The SSM parameter containing the Anthropic API key
- The CloudFormation stacks you want monitored

If your AWS CLI defaults to a different region, always pass `--region`
explicitly on every command.

---

## Opting stacks in and out

Any CloudFormation stack can be opted into monitoring by adding the tag:

```
Key:   drift-monitor
Value: enabled
```

To use a different tag convention (e.g. your team already tags stacks
with `team: platform`), set the variables at deploy time:

```bash
terraform apply \
  -var="monitor_tag_key=team" \
  -var="monitor_tag_value=platform" \
  -var="alert_email=you@example.com"
```

---

## Findings severity

| Severity | Meaning | Example |
|---|---|---|
| `CRITICAL` | Immediate action needed | Runtime is EOL or blocked — functions cannot be updated or invoked |
| `WARNING` | Action needed soon | Runtime deprecated, or EOL date within 90 days |

Clean runs (no findings, no errors) only log to CloudWatch.
No email is sent on a clean run.

---

## Project structure

```
drift-monitor/
├── terraform/
│   ├── main.tf              # All AWS infrastructure
│   ├── variables.tf         # All configurable inputs
│   ├── outputs.tf           # Post-deploy info and manual trigger command
│   └── versions.tf          # Provider version pins
├── lambda/
│   ├── handler.py           # Entry point — orchestrates the full flow
│   ├── scanner.py           # Finds tagged CloudFormation stacks
│   ├── eol_scraper.py       # Scrapes AWS docs for runtime EOL data
│   ├── ai_parser.py         # Uses Claude to parse HTML into structured JSON
│   ├── eol_cache.py         # S3 cache manager for EOL data
│   ├── notifier.py          # SNS + CloudWatch reporting
│   ├── requirements.txt     # Python dependencies (requests only)
│   └── checks/
│       ├── __init__.py
│       └── lambda_runtime.py  # Lambda runtime EOL check
├── tests/
│   └── test_lambda_runtime.py  # Unit tests (no AWS credentials needed)
├── scripts/
│   ├── build.ps1            # Lambda packaging script — Windows
│   └── build.sh             # Lambda packaging script — Linux/Mac
├── private-bucket.yaml      # Sample CloudFormation stack for testing
└── README.md
```

---

## Running tests locally

No AWS credentials needed — all AWS calls are mocked.

```bash
pip install pytest boto3
pytest tests/ -v
```

---

## Adding new checks

1. Create `lambda/checks/your_check.py` following the pattern in `lambda_runtime.py`
   - Accept `(stack, eol_data, config)` as parameters
   - Return a list of Finding dicts (empty list if no issues found)
2. Import and call it in `handler.py` inside the stack loop
3. Add tests in `tests/`
4. Run `terraform apply` — the build script picks up the new file automatically

---

## Design decisions

| Decision | Choice | Reason |
|---|---|---|
| Scrape vs API | Scrape AWS docs | No AWS API exposes runtime EOL dates |
| Parse method | Claude AI (bounded) | Resilient to page layout changes; BeautifulSoup would need updating every time AWS changes the docs page structure |
| AI bounding | Strict system prompt + JSON validation + markdown fence stripping | Model occasionally wraps JSON in code fences despite instructions — we strip and validate rather than fail |
| Cache location | S3 | Persistent across invocations, versioned, cheap |
| Secret storage | SSM SecureString | Encrypted at rest and in transit — never appears in env vars, Lambda config, or Terraform state |
| Runtime check source | Lambda API (live) | CloudFormation templates can lag behind the actual deployed state — we check what is really running |
| Remediation | Flag only (Phase 1) | Auto-remediation needs rigorous dev/prod environment logic before it is safe to ship |
| Notification trigger | Findings and errors only | Clean-run emails are noise; CloudWatch logs confirm clean runs |
| Cross-platform build | Two scripts (.ps1 and .sh) | Windows and Linux/Mac require different shell interpreters |

---

## Roadmap

- [x] Phase 1: Lambda runtime checks, SNS + CloudWatch alerts, S3 EOL cache
- [ ] Phase 2: Self-monitoring alarms (alert if the Lambda itself errors)
- [ ] Phase 3: RDS engine version and AMI age checks
- [ ] Phase 4: Slack/Teams webhook alongside SNS email
- [ ] Phase 5: Auto-remediation for safe fixes
