# Chat service

Requires AWS CLI, Node.js 22+, Serverless Framework v4, and credentials that can deploy Lambda, DynamoDB, S3, and API Gateway integrations. The existing API must be a WebSocket API with route selection expression `$request.body.action`.

Run from PowerShell:

```powershell
.\deploy.ps1 -ApiId YOUR_API_ID -Region ap-southeast-1 -PlayerPoolId YOUR_PLAYER_POOL_ID
```

The script deploys the three Python Lambdas and private DynamoDB/S3 resources, then attaches `$connect`, `$disconnect`, `$default`, and the chat actions to the existing API. It creates integrations and routes if absent and updates routes if present. The `-Stage` option defaults to `prod`. Set `NEXT_PUBLIC_CHAT_WS_URL` in `revamp/.env.local` to the printed `wss://` URL and rebuild. Set `CHAT_TABLE` in the admin deployment to the stack's `ChatTableName` output. The admin runtime needs `dynamodb:Scan`, `dynamodb:GetItem`, and `dynamodb:UpdateItem` on that table.

Clients authenticate over the socket with a Cognito **access token**; the Lambda verifies it with Cognito `GetUser`. The player app client must allow `aws.cognito.signin.user.admin`, and players with older sessions must sign in again. No bearer token is placed in the URL. Media uploads use short lived S3 PUT URLs and are checked again with `HeadObject` before a message is accepted. S3 remains private; reads use one hour presigned URLs. The 5 MB limit applies to uploaded image and video objects. The app can share soundboard clips as sound IDs; the Lambda validates them against `sound-ids.json`. Update that file when the catalog in `revamp/src/data/legacy.json` changes, then redeploy this service. Existing GIF messages still display, but the app currently hides the GIF link composer. Set both `CHAT_TABLE` and `CHAT_BUCKET` from the CloudFormation outputs in the admin deployment; its IAM role needs DynamoDB report read/update and S3 GetObject on `chat/*`.

Presigned media URLs are generated against the bucket's regional S3 endpoint. If a browser upload is redirected from `s3.amazonaws.com`, redeploy this service so the updated Lambda signs the regional host directly; S3 preflight requests cannot rely on a redirect.

Chat restrictions are stored as `CHAT_RESTRICTION#<Cognito sub>` items in the chat table. The action Lambda checks this item before creating a conversation, issuing an upload URL, or sending a message. Restricted players can still read conversations and report abuse. The admin app uses `dynamodb:UpdateItem` to restrict and unrestrict players; the action Lambda already has `dynamodb:GetItem`. Redeploy this service after updating the handler.

The table uses on demand billing. Before production, set CloudWatch alarms, retention, and S3 lifecycle policies appropriate to your moderation policy. The current report workflow records reports and lets admins resolve them; it does not automatically remove reported messages.
