#!/bin/bash
# Deploy trading dashboard to S3 + CloudFront
# This creates a publicly accessible URL like https://xxx.cloudfront.net

set -e

BUCKET_NAME="btc-trading-dashboard-$(date +%s)"
REGION="us-east-1"

echo "🚀 Deploying BTC Trading Dashboard to AWS"
echo "==========================================="

# 1. Create S3 bucket
echo "📦 Creating S3 bucket: $BUCKET_NAME"
aws s3 mb s3://$BUCKET_NAME --region $REGION

# 2. Enable static website hosting
echo "🌐 Enabling static website hosting"
aws s3 website s3://$BUCKET_NAME --index-document dashboard.html

# 3. Make bucket public
echo "🔓 Setting bucket policy for public access"
cat > /tmp/bucket-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::$BUCKET_NAME/*"
    }
  ]
}
EOF

aws s3api put-bucket-policy --bucket $BUCKET_NAME --policy file:///tmp/bucket-policy.json

# 4. Upload dashboard files
echo "📤 Uploading dashboard files"
aws s3 cp dashboard.html s3://$BUCKET_NAME/dashboard.html --content-type "text/html"

# 5. Create CloudFront distribution
echo "☁️  Creating CloudFront distribution (this takes ~5 minutes)"
DISTRIBUTION_CONFIG=$(cat <<EOF
{
  "CallerReference": "btc-dashboard-$(date +%s)",
  "Comment": "BTC Trading Dashboard",
  "Enabled": true,
  "Origins": {
    "Quantity": 1,
    "Items": [
      {
        "Id": "S3-$BUCKET_NAME",
        "DomainName": "$BUCKET_NAME.s3.$REGION.amazonaws.com",
        "S3OriginConfig": {
          "OriginAccessIdentity": ""
        }
      }
    ]
  },
  "DefaultRootObject": "dashboard.html",
  "DefaultCacheBehavior": {
    "TargetOriginId": "S3-$BUCKET_NAME",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {
      "Quantity": 2,
      "Items": ["GET", "HEAD"]
    },
    "ForwardedValues": {
      "QueryString": false,
      "Cookies": {
        "Forward": "none"
      }
    },
    "MinTTL": 0,
    "TrustedSigners": {
      "Enabled": false,
      "Quantity": 0
    }
  }
}
EOF
)

DISTRIBUTION_ID=$(aws cloudfront create-distribution --distribution-config "$DISTRIBUTION_CONFIG" --query 'Distribution.Id' --output text)
CLOUDFRONT_URL=$(aws cloudfront get-distribution --id $DISTRIBUTION_ID --query 'Distribution.DomainName' --output text)

echo ""
echo "✅ Deployment Complete!"
echo "==========================================="
echo "S3 Bucket: $BUCKET_NAME"
echo "CloudFront URL: https://$CLOUDFRONT_URL"
echo ""
echo "⏳ Note: CloudFront distribution takes 5-15 minutes to deploy globally"
echo "   You can check status with: aws cloudfront get-distribution --id $DISTRIBUTION_ID"
echo ""
echo "🔄 To update the dashboard later:"
echo "   aws s3 cp dashboard.html s3://$BUCKET_NAME/dashboard.html"
echo "   aws cloudfront create-invalidation --distribution-id $DISTRIBUTION_ID --paths '/*'"
