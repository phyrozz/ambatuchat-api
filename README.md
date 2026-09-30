# Chat service

Friend requests use the push Lambda already deployed with this service. After deploying this version, set `CHAT_PUSH_FUNCTION_NAME` in the admin environment to the stage's push Lambda name and grant the admin runtime `lambda:InvokeFunction` on it. New incoming requests notify recipients who opted in to push and open `/friends/`; conversation mute settings do not suppress these request alerts. The in-app request badge is available even without push permission.

Requires AWS CLI, Node.js 22+, Serverless Framework v4, and credentials that can deploy Lambda, DynamoDB, S3, and API Gateway integrations. The existing API must be a WebSocket API with route selection expression `$request.body.action`.

Run from PowerShell:

```powershell
.\deploy.ps1 -ApiId YOUR_API_ID -Region ap-southeast-1 -PlayerPoolId YOUR_PLAYER_POOL_ID
```

The script deploys five Python Lambdas and private DynamoDB/S3 resources, then attaches `$connect`, `$disconnect`, `$default`, and the chat actions to the selected WebSocket API. The cleanup and push Lambdas are invoked internally and have no WebSocket routes. It creates integrations and routes if absent and updates routes if present. The `-Stage` option defaults to `prod`.

For an isolated development environment in the same AWS account/profile, use `-Stage dev -CreateApi`. This creates (or reuses) a dedicated WebSocket API named `ambatu-chat-dev`; the dev routes will not replace the production API's integrations. The API Gateway API is managed separately from the Serverless CloudFormation stack, so delete it separately with `aws apigatewayv2 delete-api --api-id DEV_API_ID --region YOUR_REGION` when retiring the dev environment. The Serverless stack, Lambda names, DynamoDB table, and S3 bucket are stage-specific. Use the same player pool ID if developers should sign in with the existing player accounts:

```powershell
.\deploy.ps1 -CreateApi -Stage dev -Region ap-southeast-1 -PlayerPoolId YOUR_PLAYER_POOL_ID -AwsProfile YOUR_EXISTING_PROFILE
```

Alternatively, pass an already-created development API ID with `-ApiId` and omit `-CreateApi`. Do not pass the production API ID for a second stage; API Gateway routes are shared by stages, and this script updates their integrations. Set `NEXT_PUBLIC_CHAT_WS_URL` in the matching revamp environment to the printed `wss://` URL and rebuild. Set `CHAT_TABLE` in the matching admin deployment to the stage's `ChatTableName` output. The admin runtime needs `dynamodb:Scan`, `dynamodb:GetItem`, and `dynamodb:UpdateItem` on that table.

For this local dev deployment, `admin/.env.development.local` selects the deployment AWS profile and points the admin APIs at the dev table and media bucket, and `revamp/.env.development.local` points the app at the dev WebSocket endpoint. Next.js loads these files for `npm run dev` and continues to read other settings from `.env.local`. They are local-only overrides; `.env.local` and production settings remain unchanged.

Clients authenticate over the socket with a Cognito **access token**; the Lambda verifies it with Cognito `GetUser`. The player app client must allow `aws.cognito.signin.user.admin`, and players with older sessions must sign in again. No bearer token is placed in the URL. Media uploads use short lived S3 PUT URLs and are checked again with `HeadObject` before a message is accepted. S3 remains private; reads use one hour presigned URLs. The 5 MB limit applies to uploaded image and video objects. Sound messages carry a bounded sound ID; the client picker loads published IDs and playback URLs from the admin sound catalog. Redeploy this service once to replace the previous sound ID allowlist; after that, new admin sounds need no chat-service changes or redeploys. Existing GIF messages still display, but the app currently hides the GIF link composer. Set both `CHAT_TABLE` and `CHAT_BUCKET` from the CloudFormation outputs in the admin deployment; its IAM role needs DynamoDB report read/update and S3 GetObject on `chat/*`.

Presigned media URLs are generated against the bucket's regional S3 endpoint. If a browser upload is redirected from `s3.amazonaws.com`, redeploy this service so the updated Lambda signs the regional host directly; S3 preflight requests cannot rely on a redirect.

Chat restrictions are stored as `CHAT_RESTRICTION#<Cognito sub>` items in the chat table. The action Lambda checks this item before creating a conversation, issuing an upload URL, or sending a message. Restricted players can still read conversations and report abuse. The admin app uses `dynamodb:UpdateItem` to restrict and unrestrict players; the action Lambda already has `dynamodb:GetItem`. Redeploy this service after updating the handler.

Initial message history and older pages use batches of 50, ordered oldest to newest in the response. Clients pass `nextCursor` to the `history` action to load earlier messages. Clients with cached history can pass `after` with their newest message key; the service returns only newer messages and uses `newerCursor` when another batch is needed. `refreshMedia` accepts up to 100 cached message keys and returns renewed signed URLs for media whose previous links expired. Deploy this service before releasing the matching revamp client.

Group members can add players by username and remove members (including themselves) through `addGroupMember` and `removeGroupMember`. Membership changes update the group and each player's conversation record in one DynamoDB transaction, then notify connected players. A group supports up to 25 members. All current members can manage membership, and newly added players can see earlier messages. When the final member leaves, the group and its inbox entry are deleted immediately; an asynchronous cleanup Lambda removes its message history and uploaded media. Reports already submitted to admins are retained. Deploy the service to add both WebSocket routes before releasing the matching revamp UI.

Group members can create seven-day share links with `createGroupInvite`. Signed-in visitors use `previewGroupInvite` to see the group name and member count, then explicitly join with `acceptGroupInvite`. The invite token is stored in DynamoDB with a TTL, but its expiry is also checked at request time. Existing members can open the group from the same link. The revamp profile shares a direct-message link and QR code; its destination opens `/chat/` after Google sign-in.

In group chats, typing `@` in the composer offers searchable group members. Selecting a member attaches their ID to the outgoing message, and their push notification identifies the sender and includes the message preview. Each conversation can be muted or unmuted independently with the `setMute` action; muted conversations skip push delivery while live chat continues. Deploy the service and route before releasing the matching revamp UI.

Message reactions use the `react` WebSocket action and are stored on each message as one emoji per player. Calling `react` with the same emoji removes it; another emoji replaces it. The action broadcasts the updated reaction map to everyone in the conversation. The picker and Lambda validate against Unicode Emoji 18.0, generated from [Unicode's emoji-test.txt](https://www.unicode.org/Public/emoji/latest/emoji-test.txt). To update both catalogs, download that file and run `python tools/generate-chat-emojis.py path/to/emoji-test.txt` from the workspace root. Redeploy this service to install the new `react` route and Lambda code.

The table uses on demand billing. Before production, set CloudWatch alarms, retention, and S3 lifecycle policies appropriate to your moderation policy. The current report workflow records reports and lets admins resolve them; it does not automatically remove reported messages.

## Message push notifications

Push tokens are stored in the existing chat table under `PUSH#<Cognito sub>` after a signed-in player opts in from the Ambatuchat inbox. After persisting a message, the action Lambda asynchronously invokes the push Lambda for the other conversation members, so FCM delivery does not delay live chat. Text notifications include a preview of up to 120 characters; group notifications include the sender's name and group title. Selected group mentions add a mention label and include the message preview. Muted conversations skip push delivery per recipient. Other message types use a generic preview. Notifications are sent only to other conversation members, not the sender. Invalid tokens are removed after FCM reports them as expired. Reactions and group membership updates do not generate notifications.

Configure Firebase Cloud Messaging before deploying:

1. Create a Firebase project and enable the Firebase Cloud Messaging API. Register Android and iOS apps using the app's bundle/application ID (`com.example.ambatuapp` in the current Capacitor config). Download `google-services.json` to `revamp/android/app/` and add `GoogleService-Info.plist` to the iOS App target in Xcode.
2. In Firebase project settings, create a service account with the Firebase Cloud Messaging API Admin role. Store the downloaded service-account JSON as an AWS Secrets Manager secret named `ambatu-chat/prod/fcm` (or the matching stage). Do not commit either service-account file.
3. Upload an APNs authentication key in Firebase project settings for iOS delivery. Enable Push Notifications for the iOS App ID and use a physical iOS device for testing.
4. For web push, add the deployed website hostname to Firebase's authorized domains and generate a Web Push certificate key pair. Add the Firebase web app config and public VAPID key to `revamp/.env.local` using the `NEXT_PUBLIC_FIREBASE_*` variables in `revamp/.env.example`. Set `NEXT_PUBLIC_APP_URL` to the HTTPS origin used by the website.
5. Set `FCM_PROJECT_ID`, `FCM_SECRET_ARN` (the Secrets Manager ARN), and `WEB_APP_URL` in the shell used for `deploy.ps1`, then deploy chat-service. Set the same Firebase web app config in the production web build environment and rebuild the web/native app.

The service account JSON is read at runtime from Secrets Manager. The deployment role needs Docker to build the Python `google-auth` and `requests` dependencies for the Lambda's Linux ARM64 runtime. Configure all app credentials before running `npm run cap:sync`; then install the rebuilt app and tap the bell in Ambatuchat to grant permission and register that device. Web push requires HTTPS. On iOS web, visitors must add the site to their Home Screen before enabling notifications.
