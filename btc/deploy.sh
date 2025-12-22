#!/bin/bash
# Deploy BTC Dashboard to S3 and Lambda
# Run manually with: ./btc/deploy.sh
# Or automatically via GitHub Actions on push

set -e

BUCKET="btc-trading-dashboard-1765598917"
REGION="us-east-1"
LAMBDA_NAME="BTCDashboardGenerator"

echo "🚀 Deploying BTC Dashboard..."
echo "================================"

# Navigate to btc directory
cd "$(dirname "$0")"

# 1. Upload dashboard HTML to S3
echo "📤 Uploading dashboard.html to S3..."
aws s3 cp dashboard.html "s3://$BUCKET/dashboard.html" \
  --content-type "text/html" \
  --cache-control "no-cache, no-store, must-revalidate" \
  --region $REGION

# 2. Create Lambda deployment package
echo "📦 Creating Lambda package..."
cd lambda_package
rm -f ../dashboard_lambda.zip
zip -r ../dashboard_lambda.zip . -x "*.pyc" -x "__pycache__/*" -x "*.so"
cd ..

# 3. Update Lambda function
echo "⬆️  Updating Lambda function..."
aws lambda update-function-code \
  --function-name $LAMBDA_NAME \
  --zip-file fileb://dashboard_lambda.zip \
  --region $REGION \
  --no-cli-pager

# 4. Wait for Lambda to be ready
echo "⏳ Waiting for Lambda update to complete..."
aws lambda wait function-updated \
  --function-name $LAMBDA_NAME \
  --region $REGION

# 5. Cleanup
rm -f dashboard_lambda.zip

echo ""
echo "✅ Deployment complete!"
echo "================================"
echo "Dashboard: https://$BUCKET.s3.$REGION.amazonaws.com/dashboard.html"
echo "Lambda: $LAMBDA_NAME updated"
