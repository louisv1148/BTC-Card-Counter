#!/bin/bash

# Deploy Kalshi Weather Trading Bot to AWS Lambda

set -e

FUNCTION_NAME="KalshiWeatherTradingBot"
REGION="us-east-1"

echo "================================"
echo "Kalshi Trading Bot Deployment"
echo "================================"

# Get script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Step 1: Create deployment package
echo ""
echo "Step 1: Creating deployment package..."
cd "$PROJECT_ROOT/lambda_package"
rm -f "$PROJECT_ROOT/lambda_function.zip"

# Package everything
zip -r "$PROJECT_ROOT/lambda_function.zip" . -x "*.pyc" -x "__pycache__/*" -x "*.git*"

cd "$PROJECT_ROOT"
echo "✅ Deployment package created: lambda_function.zip"

# Step 2: Check if Lambda function exists
echo ""
echo "Step 2: Checking if Lambda function exists..."
if aws lambda get-function --function-name $FUNCTION_NAME --region $REGION 2>/dev/null; then
    echo "Function exists, updating code..."
    aws lambda update-function-code \
        --function-name $FUNCTION_NAME \
        --zip-file fileb://lambda_function.zip \
        --region $REGION

    echo "✅ Lambda function code updated!"
else
    echo "Function doesn't exist. Please create it first with:"
    echo ""
    echo "aws lambda create-function \\"
    echo "    --function-name $FUNCTION_NAME \\"
    echo "    --runtime python3.11 \\"
    echo "    --role arn:aws:iam::YOUR_ACCOUNT_ID:role/lambda-execution-role \\"
    echo "    --handler lambda_function.lambda_handler \\"
    echo "    --zip-file fileb://lambda_function.zip \\"
    echo "    --timeout 300 \\"
    echo "    --memory-size 512 \\"
    echo "    --region $REGION"
    exit 1
fi

# Step 3: Update environment variables
echo ""
echo "Step 3: Updating environment variables..."
echo "Please set these manually in AWS Lambda Console:"
echo "  - KALSHI_KEY_ID: Your Kalshi API Key ID"
echo "  - KALSHI_PRIVATE_KEY: Your Kalshi Private Key (single line, no newlines)"
echo ""
echo "Or run:"
echo "aws lambda update-function-configuration \\"
echo "    --function-name $FUNCTION_NAME \\"
echo "    --environment Variables=\"{KALSHI_KEY_ID=your-key-id,KALSHI_PRIVATE_KEY=your-private-key}\" \\"
echo "    --region $REGION"

echo ""
echo "================================"
echo "Deployment Summary"
echo "================================"
echo "Function: $FUNCTION_NAME"
echo "Region: $REGION"
echo "Package: lambda_function.zip"
echo ""
echo "Next steps:"
echo "1. Set environment variables (KALSHI_KEY_ID, KALSHI_PRIVATE_KEY)"
echo "2. Create KalshiTrades DynamoDB table: ./scripts/setup_dynamodb.sh"
echo "3. Set up EventBridge triggers: ./scripts/setup_eventbridge.sh"
echo "4. Test the function: aws lambda invoke --function-name $FUNCTION_NAME output.json"
echo ""
echo "✅ Deployment complete!"
