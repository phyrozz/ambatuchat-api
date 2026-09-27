"""Mobile and web push registration and delivery."""
import hashlib
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from boto3.dynamodb.conditions import Key

from chat_runtime import ddb, member, now, secrets

_fcm_credentials = None

def register_push(user, body, adding):
    token = body.get('token')
    platform = body.get('platform')
    locale = body.get('locale', 'en')
    if not isinstance(token, str) or not 20 <= len(token) <= 4096 or not isinstance(platform, str) or platform not in ('android', 'ios', 'web') or locale not in ('en', 'id'):
        raise ValueError('Invalid push registration.')
    key = {'pk': f'PUSH#{user}', 'sk': f'TOKEN#{hashlib.sha256(token.encode()).hexdigest()}'}
    if adding:
        ddb.put_item(Item={**key, 'token': token, 'platform': platform, 'locale': locale, 'updatedAt': now()})
    else:
        ddb.delete_item(Key=key)
    return {'registered': adding}

def fcm_access_token():
    global _fcm_credentials
    if not _fcm_credentials:
        arn = os.environ.get('FCM_SECRET_ARN')
        project = os.environ.get('FCM_PROJECT_ID')
        if not arn or not project:
            return None, None
        secret = json.loads(secrets.get_secret_value(SecretId=arn)['SecretString'])
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import service_account
        _fcm_credentials = service_account.Credentials.from_service_account_info(secret, scopes=['https://www.googleapis.com/auth/firebase.messaging'])
        _fcm_credentials.refresh(GoogleRequest())
    elif not _fcm_credentials.valid or _fcm_credentials.expired:
        from google.auth.transport.requests import Request as GoogleRequest
        _fcm_credentials.refresh(GoogleRequest())
    return _fcm_credentials.token, os.environ['FCM_PROJECT_ID']

def notification_preview(kind, text):
    if kind != 'text' or not isinstance(text, str) or not text.strip():
        return 'You have a new message.'
    preview = ' '.join(text.split())
    if len(preview) > 120:
        preview = preview[:119].rstrip() + '…'
    return preview

def send_push_notifications(user, conversation, event):
    try:
        conversation_record = member(conversation, user)
        if conversation_record and conversation_record.get('muted') is True:
            print(f'Push delivery skipped for muted conversation {conversation}.')
            return
        access_token, project = fcm_access_token()
        if not access_token:
            return
        devices = ddb.query(KeyConditionExpression=Key('pk').eq(f'PUSH#{user}') & Key('sk').begins_with('TOKEN#')).get('Items', [])
        print(f'Push delivery found {len(devices)} registered device(s).')
        preview = notification_preview(event.get('kind'), event.get('text', ''))
        sender_name = str(event.get('senderName') or '').strip()
        is_group = bool(event.get('group'))
        title = str(event.get('groupTitle') or '').strip() if is_group else sender_name
        if not title:
            title = 'New message'
        is_mentioned = user in event.get('mentions', [])
        body = f'{sender_name}: {preview}' if is_group and sender_name else preview
        for device in devices:
            device_body = f'{sender_name} mentioned you: {preview}' if is_mentioned and sender_name else body
            payload = json.dumps({'message': {
                'token': device['token'],
                'notification': {'title': title, 'body': device_body},
                'data': {'conversationId': conversation, 'url': f'/chat/?conversation={conversation}'},
                'webpush': {'data': {'conversationId': conversation}, 'fcm_options': {'link': f"{os.environ['WEB_APP_URL'].rstrip('/')}/chat/?conversation={conversation}"} if os.environ.get('WEB_APP_URL') else {}, 'notification': {'icon': '/app-icon.svg', 'tag': f'chat-{conversation}'}},
                'android': {'notification': {'channel_id': 'messages', 'tag': f'chat-{conversation}'}},
            }}).encode()
            request = Request(f'https://fcm.googleapis.com/v1/projects/{project}/messages:send', data=payload, headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}, method='POST')
            try:
                with urlopen(request, timeout=5):
                    print('FCM accepted a push notification.')
            except HTTPError as error:
                if b'UNREGISTERED' in error.read():
                    ddb.delete_item(Key={'pk': device['pk'], 'sk': device['sk']})
                else:
                    print(f'FCM delivery failed: {error}')
    except Exception as error:
        print(f'Push delivery unavailable: {error}')

def push_notifications(event, _context):
    from concurrent.futures import ThreadPoolExecutor
    users = event.get('users', [])
    conversation = event.get('conversation', '')
    with ThreadPoolExecutor(max_workers=8) as executor:
        tasks = [executor.submit(send_push_notifications, user, conversation, event) for user in users if isinstance(user, str)]
        for task in tasks:
            task.result()
