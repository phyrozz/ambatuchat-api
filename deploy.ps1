param(
  [Parameter(Mandatory = $true)][string]$ApiId,
  [Parameter(Mandatory = $true)][string]$Region,
  [Parameter(Mandatory = $true)][string]$PlayerPoolId,
  [string]$Stage = 'prod',
  [string]$AwsProfile = ''
)
$ErrorActionPreference = 'Stop'
function Get-AwsJsonMaybeMissing {
  param([string[]]$Arguments)
  # Windows PowerShell 5.1 turns AWS CLI stderr into a terminating
  # NativeCommandError when ErrorActionPreference is Stop.
  $priorPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $output = & aws @Arguments 2>&1
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $priorPreference
  }
  $text = ($output | ForEach-Object { $_.ToString() }) -join "`n"
  if ($exitCode -eq 0) { return $text | ConvertFrom-Json }
  if ($text -match 'NotFoundException|ResourceNotFoundException') { return $null }
  throw "AWS CLI failed: $text"
}
$env:AWS_REGION = $Region
$env:PLAYER_POOL_ID = $PlayerPoolId
if ($AwsProfile) { $env:AWS_PROFILE = $AwsProfile }
$api = aws apigatewayv2 get-api --api-id $ApiId --region $Region | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $api.ProtocolType -ne 'WEBSOCKET' -or $api.RouteSelectionExpression -ne '$request.body.action') {
  throw 'The API must be a WebSocket API using $request.body.action.'
}
$env:WEBSOCKET_ENDPOINT = "https://$ApiId.execute-api.$Region.amazonaws.com/$Stage"
$accountId = aws sts get-caller-identity --query Account --output text --region $Region
if ($LASTEXITCODE -ne 0) { throw 'AWS credentials are not available.' }
$env:WEBSOCKET_MANAGE_ARN = "arn:aws:execute-api:${Region}:${accountId}:${ApiId}/${Stage}/POST/@connections/*"
$configuredStage = Get-AwsJsonMaybeMissing -Arguments @('apigatewayv2', 'get-stage', '--api-id', $ApiId, '--stage-name', $Stage, '--region', $Region)
if (-not $configuredStage) {
  aws apigatewayv2 create-stage --api-id $ApiId --stage-name $Stage --auto-deploy --region $Region | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Could not create the $Stage stage." }
}
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
  if (-not (Test-Path node_modules)) { npm install }
  if ($LASTEXITCODE -ne 0) { throw 'npm install failed.' }
  npx serverless deploy --stage $Stage --region $Region
  if ($LASTEXITCODE -ne 0) { throw 'Serverless deployment failed.' }
  $routes = @('$connect', '$disconnect', 'authenticate', 'conversations', 'createConversation', 'addGroupMember', 'removeGroupMember', 'createGroupInvite', 'previewGroupInvite', 'acceptGroupInvite', 'history', 'mediaUpload', 'send', 'react', 'report', 'registerPush', 'unregisterPush', '$default')
  foreach ($route in $routes) {
    $handler = if ($route -eq '$connect') { 'connect' } elseif ($route -eq '$disconnect') { 'disconnect' } else { 'action' }
    $functionName = "ambatu-chat-$Stage-$handler"
    $functionArn = "arn:aws:lambda:${Region}:${accountId}:function:$functionName"
    $statement = "chat-$ApiId-$Stage-$handler"
    $sourceArn = "arn:aws:execute-api:${Region}:${accountId}:${ApiId}/*"
    $policyText = Get-AwsJsonMaybeMissing -Arguments @('lambda', 'get-policy', '--function-name', $functionName, '--region', $Region)
    $policyStatements = if ($policyText) { ($policyText.Policy | ConvertFrom-Json).Statement } else { @() }
    if (-not ($policyStatements | Where-Object { $_.Sid -eq $statement })) {
      aws lambda add-permission --function-name $functionName --statement-id $statement --action lambda:InvokeFunction --principal apigateway.amazonaws.com --source-arn $sourceArn --region $Region | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "Could not grant API Gateway access to $functionName." }
    }
    $uri = "arn:aws:apigateway:${Region}:lambda:path/2015-03-31/functions/$functionArn/invocations"
    $routeList = aws apigatewayv2 get-routes --api-id $ApiId --region $Region | ConvertFrom-Json
    $existing = $routeList.Items | Where-Object { $_.RouteKey -eq $route } | Select-Object -First 1
    $integrationId = if ($existing.Target -match '^integrations/([A-Za-z0-9]+)$') { $Matches[1] } else { $null }
    if ($integrationId) {
      aws apigatewayv2 update-integration --api-id $ApiId --integration-id $integrationId --integration-uri $uri --region $Region | Out-Null
    } else {
      $integration = aws apigatewayv2 create-integration --api-id $ApiId --integration-type AWS_PROXY --integration-method POST --integration-uri $uri --region $Region | ConvertFrom-Json
      $integrationId = $integration.IntegrationId
    }
    if ($LASTEXITCODE -ne 0 -or -not $integrationId) { throw "Could not configure integration for $route." }
    if ($existing) {
      aws apigatewayv2 update-route --api-id $ApiId --route-id $existing.RouteId --target "integrations/$integrationId" --region $Region | Out-Null
    } else {
      aws apigatewayv2 create-route --api-id $ApiId --route-key $route --target "integrations/$integrationId" --region $Region | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not configure $route." }
  }
  $configuredStage = aws apigatewayv2 get-stage --api-id $ApiId --stage-name $Stage --region $Region | ConvertFrom-Json
  if ($configuredStage -and -not $configuredStage.AutoDeploy) {
    aws apigatewayv2 create-deployment --api-id $ApiId --stage-name $Stage --region $Region | Out-Null
  }
  Write-Host "Chat WebSocket: wss://$ApiId.execute-api.$Region.amazonaws.com/$Stage"
  Write-Host 'Set NEXT_PUBLIC_CHAT_WS_URL to this address in revamp and rebuild the app.'
  Write-Host 'Set CHAT_TABLE in the admin app from the Serverless stack ChatTableName output.'
  Write-Host 'Set CHAT_BUCKET in the admin app from the Serverless stack ChatBucketName output.'
} finally {
  Pop-Location
}
