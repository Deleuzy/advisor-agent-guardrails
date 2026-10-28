#!/usr/bin/env bash
# Removes what deploy_ecs_fargate.sh created, so nothing keeps costing money.
set -uo pipefail
REGION="${AWS_REGION:-eu-north-1}"
NAME="advisor-agent"
aws ecs update-service --region "$REGION" --cluster "$NAME" --service "$NAME" --desired-count 0 >/dev/null
aws ecs delete-service --region "$REGION" --cluster "$NAME" --service "$NAME" --force >/dev/null
aws ecs wait services-inactive --region "$REGION" --cluster "$NAME" --services "$NAME"
aws ecs delete-cluster --region "$REGION" --cluster "$NAME" >/dev/null
VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 describe-security-groups --region "$REGION" --filters Name=group-name,Values="$NAME" Name=vpc-id,Values="$VPC" --query 'SecurityGroups[0].GroupId' --output text)
[ "$SG" != "None" ] && aws ec2 delete-security-group --region "$REGION" --group-id "$SG"
aws logs delete-log-group --region "$REGION" --log-group-name "/ecs/$NAME"
for td in $(aws ecs list-task-definitions --region "$REGION" --family-prefix "$NAME" --query 'taskDefinitionArns[]' --output text); do
  aws ecs deregister-task-definition --region "$REGION" --task-definition "$td" >/dev/null
done
aws iam detach-role-policy --role-name "${NAME}-exec" --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam delete-role --role-name "${NAME}-exec"
echo "removed"
