#!/bin/bash
#
# Setup DynamoDB table for HF Bot position persistence
#

set -e

REGION="${AWS_REGION:-us-east-1}"
TABLE_NAME="BTCHFPositions"

echo "Creating DynamoDB table: $TABLE_NAME in $REGION"

aws dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --attribute-definitions \
        AttributeName=pk,AttributeType=S \
        AttributeName=sk,AttributeType=S \
    --key-schema \
        AttributeName=pk,KeyType=HASH \
        AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --region "$REGION"

echo "Waiting for table to become active..."
aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"

echo "✅ Table $TABLE_NAME created successfully!"
