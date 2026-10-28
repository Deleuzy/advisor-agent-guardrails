#!/usr/bin/env bash
# Deploy the published container image to AWS ECS Fargate with the AWS CLI.
# Not run by CI and never run so far. Needs: an AWS account, `aws configure` done by you,
# and the GHCR package ghcr.io/deleuzy/advisor-agent-guardrails set to public.
# No Docker needed on your machine: the image is built and pushed by GitHub Actions.
set -euo pipefail

REGION="${AWS_REGION:-eu-north-1}"
NAME="advisor-agent"
IMAGE="${IMAGE:-ghcr.io/deleuzy/advisor-agent-guardrails:latest}"
MY_IP="$(curl -fs https://checkip.amazonaws.com)/32"   # only your IP may reach the demo

aws sts get-caller-identity >/dev/null || { echo "run 'aws configure' first"; exit 1; }
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

echo "1/6 log group and cluster"
aws logs create-log-group --log-group-name "/ecs/$NAME" --region "$REGION" 2>/dev/null || true
aws ecs create-cluster --cluster-name "$NAME" --region "$REGION" >/dev/null

echo "2/6 task execution role"
aws iam get-role --role-name "${NAME}-exec" >/dev/null 2>&1 || {
  aws iam create-role --role-name "${NAME}-exec" --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "${NAME}-exec" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
  sleep 10   # IAM is eventually consistent
}

echo "3/6 task definition (0.25 vCPU, 512 MB, mock model, no secrets)"
aws ecs register-task-definition --region "$REGION" --family "$NAME" \
  --requires-compatibilities FARGATE --network-mode awsvpc --cpu 256 --memory 512 \
  --execution-role-arn "arn:aws:iam::${ACCOUNT}:role/${NAME}-exec" \
  --container-definitions "[{
    \"name\": \"$NAME\", \"image\": \"$IMAGE\", \"essential\": true,
    \"portMappings\": [{\"containerPort\": 8000, \"protocol\": \"tcp\"}],
    \"environment\": [{\"name\": \"ADVISOR_MODEL_PROVIDER\", \"value\": \"mock\"}],
    \"logConfiguration\": {\"logDriver\": \"awslogs\", \"options\": {
      \"awslogs-group\": \"/ecs/$NAME\", \"awslogs-region\": \"$REGION\", \"awslogs-stream-prefix\": \"api\"}}}]" >/dev/null

echo "4/6 network: default VPC, security group open on 8000 to $MY_IP only"
VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --region "$REGION" --filters Name=vpc-id,Values="$VPC" --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
SG=$(aws ec2 describe-security-groups --region "$REGION" --filters Name=group-name,Values="$NAME" Name=vpc-id,Values="$VPC" \
     --query 'SecurityGroups[0].GroupId' --output text)
if [ "$SG" = "None" ]; then
  SG=$(aws ec2 create-security-group --region "$REGION" --group-name "$NAME" --description "$NAME demo" --vpc-id "$VPC" --query GroupId --output text)
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" --protocol tcp --port 8000 --cidr "$MY_IP" >/dev/null
fi

echo "5/6 service"
if aws ecs describe-services --region "$REGION" --cluster "$NAME" --services "$NAME" --query 'services[?status==`ACTIVE`]' --output text | grep -q .; then
  aws ecs update-service --region "$REGION" --cluster "$NAME" --service "$NAME" --task-definition "$NAME" --force-new-deployment >/dev/null
else
  aws ecs create-service --region "$REGION" --cluster "$NAME" --service-name "$NAME" --task-definition "$NAME" \
    --desired-count 1 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" >/dev/null
fi

echo "6/6 waiting for the task"
aws ecs wait services-stable --region "$REGION" --cluster "$NAME" --services "$NAME"
TASK=$(aws ecs list-tasks --region "$REGION" --cluster "$NAME" --service-name "$NAME" --query 'taskArns[0]' --output text)
ENI=$(aws ecs describe-tasks --region "$REGION" --cluster "$NAME" --tasks "$TASK" \
      --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" --output text)
IP=$(aws ec2 describe-network-interfaces --region "$REGION" --network-interface-ids "$ENI" \
     --query 'NetworkInterfaces[0].Association.PublicIp' --output text)
echo "Running: curl http://$IP:8000/health"
echo "Remove everything again with: deploy/aws/teardown.sh"
