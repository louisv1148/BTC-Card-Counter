#!/bin/bash
# Deploy BTC Price Collector and Trading Bot Lambdas

set -e

REGION="us-east-1"
LAMBDA_ROLE="arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/lambda-execution-role"
PACKAGE_DIR="lambda_package"
ZIP_FILE="btc_lambda.zip"

echo "=========================================="
echo "Deploying BTC Lambdas"
echo "=========================================="

# Navigate to project root
cd "$(dirname "$0")/.."

# Create deployment package
echo "Creating deployment package..."
cd $PACKAGE_DIR

# Remove old zip if exists
rm -f ../$ZIP_FILE

# Zip everything (include .so files for cryptography)
zip -r ../$ZIP_FILE . -x "*.pyc" -x "__pycache__/*"

cd ..
echo "Created $ZIP_FILE ($(du -h $ZIP_FILE | cut -f1))"

# ==========================================
# Deploy BTC Price Collector Lambda
# ==========================================
COLLECTOR_NAME="BTCPriceCollector"

echo ""
echo "Deploying $COLLECTOR_NAME..."

# Check if function exists
if aws lambda get-function --function-name $COLLECTOR_NAME --region $REGION 2>/dev/null; then
    echo "Updating existing function..."
    aws lambda update-function-code \
        --function-name $COLLECTOR_NAME \
        --zip-file fileb://$ZIP_FILE \
        --region $REGION
else
    echo "Creating new function..."
    aws lambda create-function \
        --function-name $COLLECTOR_NAME \
        --runtime python3.12 \
        --role $LAMBDA_ROLE \
        --handler btc_price_collector.lambda_handler \
        --zip-file fileb://$ZIP_FILE \
        --timeout 30 \
        --memory-size 128 \
        --region $REGION
fi

# Wait for update to complete
echo "Waiting for function to be ready..."
aws lambda wait function-updated --function-name $COLLECTOR_NAME --region $REGION 2>/dev/null || true

# ==========================================
# Deploy BTC Trading Bot Lambda
# ==========================================
TRADER_NAME="BTCTradingBot"

echo ""
echo "Deploying $TRADER_NAME..."

if aws lambda get-function --function-name $TRADER_NAME --region $REGION 2>/dev/null; then
    echo "Updating existing function..."
    aws lambda update-function-code \
        --function-name $TRADER_NAME \
        --zip-file fileb://$ZIP_FILE \
        --region $REGION
else
    echo "Creating new function..."
    aws lambda create-function \
        --function-name $TRADER_NAME \
        --runtime python3.12 \
        --role $LAMBDA_ROLE \
        --handler btc_lambda_function.lambda_handler \
        --zip-file fileb://$ZIP_FILE \
        --timeout 30 \
        --memory-size 128 \
        --region $REGION \
        --environment "Variables={KALSHI_KEY_ID=placeholder,KALSHI_PRIVATE_KEY=placeholder}"
fi

echo "Waiting for function to be ready..."
aws lambda wait function-updated --function-name $TRADER_NAME --region $REGION 2>/dev/null || true

# ==========================================
# Set up EventBridge triggers
# ==========================================
echo ""
echo "Setting up EventBridge triggers..."

# Price collector - every minute
COLLECTOR_RULE="BTCPriceCollector-EveryMinute"
echo "Creating rule: $COLLECTOR_RULE (every minute)"

aws events put-rule \
    --name $COLLECTOR_RULE \
    --schedule-expression "rate(1 minute)" \
    --state ENABLED \
    --region $REGION

# Add permission for EventBridge to invoke Lambda
aws lambda add-permission \
    --function-name $COLLECTOR_NAME \
    --statement-id "EventBridge-$COLLECTOR_RULE" \
    --action "lambda:InvokeFunction" \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:$REGION:$(aws sts get-caller-identity --query Account --output text):rule/$COLLECTOR_RULE" \
    --region $REGION 2>/dev/null || echo "Permission already exists"

# Add target
aws events put-targets \
    --rule $COLLECTOR_RULE \
    --targets "Id"="1","Arn"="arn:aws:lambda:$REGION:$(aws sts get-caller-identity --query Account --output text):function:$COLLECTOR_NAME" \
    --region $REGION

echo ""
echo "=========================================="
echo "Deployment complete!"
echo "=========================================="
echo ""
echo "Lambdas deployed:"
echo "  - $COLLECTOR_NAME (runs every minute)"
echo "  - $TRADER_NAME (invoke manually or set up H:56 trigger)"
echo ""
echo "Next steps:"
echo "  1. Set Kalshi credentials on $TRADER_NAME:"
echo "     aws lambda update-function-configuration \\"
echo "       --function-name $TRADER_NAME \\"
echo "       --environment \"Variables={KALSHI_KEY_ID=your-key,KALSHI_PRIVATE_KEY=your-key}\""
echo ""
echo "  2. Monitor price collection:"
echo "     aws logs tail /aws/lambda/$COLLECTOR_NAME --follow"
echo ""
echo "  3. Test trading bot:"
echo "     aws lambda invoke --function-name $TRADER_NAME --payload '{\"force\": true}' output.json"
