# Chat service

Requires AWS CLI, Node.js 22+, Serverless Framework v4, and credentials that can deploy Lambda, DynamoDB, S3, and API Gateway integrations. The existing API must be a WebSocket API with route selection expression `$request.body.action`.

Run from PowerShell:

```powershell
.\deploy.ps1 -ApiId YOUR_API_ID -Region ap-southeast-1 -PlayerPoolId YOUR_PLAYER_POOL_ID
```

The script deploys five Python Lambdas and private DynamoDB/S3 resources, then attaches `$connect`, `$disconnect`, `$default`, and the chat actions to the existing API. The cleanup and push Lambdas are invoked internally and have no WebSocket routes. It creates integrations and routes if absent and updates routes if present. The `-Stage` option defaults to `prod`. Set `NEXT_PUBLIC_CHAT_WS_URL` in `revamp/.env.local` to the printed `wss://` URL and rebuild. Set `CHAT_TABLE` in the admin deployment to the stack's `ChatTableName` output. The admin runtime needs `dynamodb:Scan`, `dynamodb:GetItem`, and `dynamodb:UpdateItem` on that table.

Clients authenticate over the socket with a Cognito **access token**; the Lambda verifies it with Cognito `GetUser`. The player app client must allow `aws.cognito.signin.user.admin`, and players with older sessions must sign in again. No bearer token is placed in the URL. Media uploads use short lived S3 PUT URLs and are checked again with `HeadObject` before a message is accepted. S3 remains private; reads use one hour presigned URLs. The 5 MB limit applies to uploaded image and video objects. The app can share soundboard clips as sound IDs; the Lambda validates them against `sound-ids.json`. Update that file when the catalog in `revamp/src/data/legacy.json` changes, then redeploy this service. Existing GIF messages still display, but the app currently hides the GIF link composer. Set both `CHAT_TABLE` and `CHAT_BUCKET` from the CloudFormation outputs in the admin deployment; its IAM role needs DynamoDB report read/update and S3 GetObject on `chat/*`.

Presigned media URLs are generated against the bucket's regional S3 endpoint. If a browser upload is redirected from `s3.amazonaws.com`, redeploy this service so the updated Lambda signs the regional host directly; S3 preflight requests cannot rely on a redirect.

Chat restrictions are stored as `CHAT_RESTRICTION#<Cognito sub>` items in the chat table. The action Lambda checks this item before creating a conversation, issuing an upload URL, or sending a message. Restricted players can still read conversations and report abuse. The admin app uses `dynamodb:UpdateItem` to restrict and unrestrict players; the action Lambda already has `dynamodb:GetItem`. Redeploy this service after updating the handler.

Message history is returned newest first by DynamoDB in pages of 50, then ordered oldest to newest in the response. Clients pass the returned `nextCursor` to the `history` action to load earlier messages until it is `null`.

Group members can add players by username and remove members (including themselves) through `addGroupMember` and `removeGroupMember`. Membership changes update the group and each player's conversation record in one DynamoDB transaction, then notify connected players. A group supports up to 25 members. All current members can manage membership, and newly added players can see earlier messages. When the final member leaves, the group and its inbox entry are deleted immediately; an asynchronous cleanup Lambda removes its message history and uploaded media. Reports already submitted to admins are retained. Deploy the service to add both WebSocket routes before releasing the matching revamp UI.

Group members can create seven-day share links with `createGroupInvite`. Signed-in visitors use `previewGroupInvite` to see the group name and member count, then explicitly join with `acceptGroupInvite`. The invite token is stored in DynamoDB with a TTL, but its expiry is also checked at request time. Existing members can open the group from the same link. The revamp profile shares a direct-message link and QR code; its destination opens `/chat/` after Google sign-in.

Message reactions use the `react` WebSocket action and are stored on each message as one emoji per player. Calling `react` with the same emoji removes it; another emoji replaces it. The action broadcasts the updated reaction map to everyone in the conversation. The picker and Lambda validate against Unicode Emoji 18.0, generated from [Unicode's emoji-test.txt](https://www.unicode.org/Public/emoji/latest/emoji-test.txt). To update both catalogs, download that file and run `python tools/generate-chat-emojis.py path/to/emoji-test.txt` from the workspace root. Redeploy this service to install the new `react` route and Lambda code.

The table uses on demand billing. Before production, set CloudWatch alarms, retention, and S3 lifecycle policies appropriate to your moderation policy. The current report workflow records reports and lets admins resolve them; it does not automatically remove reported messages.

## Message push notifications

Push tokens are stored in the existing chat table under `PUSH#<Cognito sub>` after a signed-in player opts in from the Ambatuchat inbox. After persisting a message, the action Lambda asynchronously invokes the push Lambda for the other conversation members, so FCM delivery does not delay live chat. Text notifications include a preview of up to 120 characters; group notifications include the sender's name and group title. Other message types use a generic preview. Notifications are sent only to other conversation members, not the sender. Invalid tokens are removed after FCM reports them as expired. Reactions and group membership updates do not generate notifications.

Configure Firebase Cloud Messaging before deploying:

1. Create a Firebase project and enable the Firebase Cloud Messaging API. Register Android and iOS apps using the app's bundle/application ID (`com.example.ambatuapp` in the current Capacitor config). Download `google-services.json` to `revamp/android/app/` and add `GoogleService-Info.plist` to the iOS App target in Xcode.
2. In Firebase project settings, create a service account with the Firebase Cloud Messaging API Admin role. Store the downloaded service-account JSON as an AWS Secrets Manager secret named `ambatu-chat/prod/fcm` (or the matching stage). Do not commit either service-account file.
3. Upload an APNs authentication key in Firebase project settings for iOS delivery. Enable Push Notifications for the iOS App ID and use a physical iOS device for testing.
4. For web push, add the deployed website hostname to Firebase's authorized domains and generate a Web Push certificate key pair. Add the Firebase web app config and public VAPID key to `revamp/.env.local` using the `NEXT_PUBLIC_FIREBASE_*` variables in `revamp/.env.example`. Set `NEXT_PUBLIC_APP_URL` to the HTTPS origin used by the website.
5. Set `FCM_PROJECT_ID`, `FCM_SECRET_ARN` (the Secrets Manager ARN), and `WEB_APP_URL` in the shell used for `deploy.ps1`, then deploy chat-service. Set the same Firebase web app config in the production web build environment and rebuild the web/native app.

The service account JSON is read at runtime from Secrets Manager. The deployment role needs Docker to build the Python `google-auth` and `requests` dependencies for the Lambda's Linux ARM64 runtime. Configure all app credentials before running `npm run cap:sync`; then install the rebuilt app and tap the bell in Ambatuchat to grant permission and register that device. Web push requires HTTPS. On iOS web, visitors must add the site to their Home Screen before enabling notifications.
