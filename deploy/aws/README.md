# Deploying to AWS (ECS Fargate)

**Status: not deployed.** These scripts have not been run against an AWS account. They are here so the
service can be put on AWS in a few minutes once an account exists. CI only checks that the scripts parse
(`bash -n`); it does not touch AWS.

Why ECS Fargate: the service is one stateless container (the request store is in memory and the audit
log is on the task's disk, see limits below), Fargate runs it without managing servers, and the image is
already built and published by GitHub Actions, so no Docker is needed on the machine that deploys.

## Steps

1. Create an AWS account and an IAM user or SSO profile with rights for ECS, EC2 (security groups),
   IAM (one role) and CloudWatch Logs. Run `aws configure` yourself.
2. On GitHub, open the package `advisor-agent-guardrails` under the profile's Packages and set its
   visibility to public (GHCR packages start private; ECS then pulls without credentials).
3. Deploy:
   ```bash
   AWS_REGION=eu-north-1 ./deploy/aws/deploy_ecs_fargate.sh
   ```
   It prints the task's public IP. The security group only lets your own IP reach port 8000.
4. Try it:
   ```bash
   curl -X POST http://<ip>:8000/requests -H 'X-Advisor-Id: A1' -H 'content-type: application/json' \
        -d '{"text": "Prepare a note for client 1042"}'
   ```
5. Remove everything: `./deploy/aws/teardown.sh`

## What a real deployment would add

- Real sign-in (the `X-Advisor-Id` header stands in for SSO) behind an Application Load Balancer with TLS.
- The audit log in a write-once store (S3 with Object Lock, or CloudWatch Logs with retention) instead of
  the container disk, and the request store in a database.
- Model keys from AWS Secrets Manager, injected as task secrets, never in the image or task definition.
- A model hosted inside the bank's boundary (for example Amazon Bedrock in an EU region) so client data
  never leaves it, even after redaction.
