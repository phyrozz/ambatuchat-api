"""API Gateway WebSocket handlers. Every data route requires a Cognito-authenticated socket."""
import base64
import json
import os
import re
import time
import uuid
import hashlib
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from decimal import Decimal
from urllib.parse import urlparse

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

ddb = boto3.resource('dynamodb').Table(os.environ['CHAT_TABLE'])
ddb_client = boto3.client('dynamodb')
serializer = TypeSerializer()
region = os.environ['AWS_REGION']
s3 = boto3.client(
    's3',
    region_name=region,
    endpoint_url=f'https://s3.{region}.amazonaws.com',
    config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}),
)
cognito = boto3.client('cognito-idp')
gateway = boto3.client('apigatewaymanagementapi', endpoint_url=os.environ['WEBSOCKET_ENDPOINT'])
with open(os.path.join(os.path.dirname(__file__), 'sound-ids.json'), encoding='utf-8') as sound_ids_file:
    SOUND_IDS = frozenset(json.load(sound_ids_file))
with open(os.path.join(os.path.dirname(__file__), 'emoji-list.json'), encoding='utf-8') as emoji_file:
    EMOJI = frozenset(json.load(emoji_file))
bucket = os.environ['CHAT_BUCKET']
cleanup_lambda = boto3.client('lambda')
push_lambda = boto3.client('lambda')
secrets = boto3.client('secretsmanager')
_fcm_credentials = None


def now():
    return int(time.time() * 1000)


def clean(item):
    if isinstance(item, Decimal):
        return int(item) if item % 1 == 0 else float(item)
    if isinstance(item, dict):
        return {key: clean(value) for key, value in item.items()}
    if isinstance(item, list):
        return [clean(value) for value in item]
    return item


def push(connection, body):
    try:
        gateway.post_to_connection(ConnectionId=connection, Data=json.dumps(clean(body)).encode())
    except gateway.exceptions.GoneException:
        ddb.delete_item(Key={'pk': f'CONN#{connection}', 'sk': 'META'})


def ok():
    return {'statusCode': 200, 'body': 'ok'}


def connect(_event, _context):
    return ok()


def disconnect(event, _context):
    connection = event['requestContext']['connectionId']
    ddb.delete_item(Key={'pk': f'CONN#{connection}', 'sk': 'META'})
    return ok()


def socket_user(connection):
    record = ddb.get_item(Key={'pk': f'CONN#{connection}', 'sk': 'META'}).get('Item')
    if not record or record.get('expiresAt', 0) <= int(time.time()):
        raise ValueError('Your chat session expired. Reconnect to continue.')
    return record['userId']


def member(conversation, user):
    return ddb.get_item(Key={'pk': f'USER#{user}', 'sk': f'CONV#{conversation}'}, ConsistentRead=True).get('Item')


def ensure_chat_allowed(user):
    restriction = ddb.get_item(Key={'pk': f'CHAT_RESTRICTION#{user}', 'sk': 'META'}).get('Item')
    if restriction and restriction.get('restricted') is True:
        raise ValueError('CHAT_RESTRICTED')


def public_message(item):
    output = {key: item[key] for key in ('id', 'conversationId', 'senderId', 'kind', 'text', 'key', 'createdAt') if key in item}
    output['messageKey'] = item['sk']
    output['reactions'] = message_reactions(item)
    if output.get('key'):
        output['url'] = s3.generate_presigned_url('get_object', Params={'Bucket': bucket, 'Key': output['key']}, ExpiresIn=3600)
    return output


def message_reactions(item):
    return {key.removeprefix('reaction_'): value for key, value in item.items() if key.startswith('reaction_') and isinstance(value, str)}


def authenticate(connection, body):
    token = body.get('token', '')
    if not isinstance(token, str) or len(token) > 10000:
        raise ValueError('Invalid sign-in token.')
    user = cognito.get_user(AccessToken=token)
    attributes = {item['Name']: item['Value'] for item in user['UserAttributes']}
    user_id = attributes['sub']
    payload = token.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    if claims.get('iss') != f'https://cognito-idp.{os.environ["AWS_REGION"]}.amazonaws.com/{os.environ["PLAYER_POOL_ID"]}':
        raise ValueError('Wrong player account.')
    expiry = int(claims['exp'])
    if expiry <= int(time.time()):
        raise ValueError('Your chat session expired.')
    ddb.put_item(Item={'pk': f'CONN#{connection}', 'sk': 'META', 'userId': user_id, 'expiresAt': expiry, 'ttl': expiry + 3600})
    return {'userId': user_id}


def conversations(user):
    result = ddb.query(KeyConditionExpression=Key('pk').eq(f'USER#{user}') & Key('sk').begins_with('CONV#'), ConsistentRead=True)
    return {'conversations': [clean(item) for item in result['Items']]}


def set_conversation_mute(user, body):
    conversation = str(body.get('conversation', ''))
    muted = body.get('muted')
    if type(muted) is not bool:
        raise ValueError('Choose whether to mute this conversation.')
    record = member(conversation, user)
    if not record:
        raise ValueError('Conversation not found.')
    ddb.update_item(
        Key={'pk': f'USER#{user}', 'sk': f'CONV#{conversation}'},
        UpdateExpression='SET muted = :muted',
        ConditionExpression='attribute_exists(pk)',
        ExpressionAttributeValues={':muted': muted},
    )
    sockets = ddb.query(IndexName='ConnectionByUser', KeyConditionExpression=Key('userId').eq(user))['Items']
    for socket in sockets:
        if socket['pk'].startswith('CONN#'):
            push(socket['pk'][5:], {'event': 'conversation', 'conversationId': conversation})
    return {'conversation': conversation, 'muted': muted}


def encoded(values):
    return {key: serializer.serialize(value) for key, value in values.items()}


def group_membership(user, body, adding):
    conversation = str(body.get('conversation', ''))
    target = body.get('userId')
    if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', target):
        raise ValueError('Choose a valid player.')
    meta_key = {'pk': f'CONV#{conversation}', 'sk': 'META'}
    meta = ddb.get_item(Key=meta_key, ConsistentRead=True).get('Item')
    membership = member(conversation, user)
    if not meta or not meta.get('group') or user not in meta['members'] or not membership:
        raise ValueError('Group chat not found.')
    old_members = meta['members']
    if adding:
        if target in old_members:
            raise ValueError('This player is already in the group.')
        if len(old_members) >= 25:
            raise ValueError('A group can have up to 25 members.')
        found = cognito.list_users(UserPoolId=os.environ['PLAYER_POOL_ID'], Filter=f'sub = "{target}"', Limit=1)
        if not found.get('Users'):
            raise ValueError('The selected player no longer exists.')
        name = body.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            raise ValueError('Choose a valid player name.')
        new_members = sorted([*old_members, target])
        names = {**membership.get('names', {}), target: name.strip()}
    else:
        if target not in old_members:
            raise ValueError('This player is not in the group.')
        if len(old_members) == 1:
            if target != user:
                raise ValueError('Only the last member can leave the group.')
            cleanup_lambda.invoke(
                FunctionName=os.environ['GROUP_CLEANUP_FUNCTION'], InvocationType='Event',
                Payload=json.dumps({'conversation': conversation}).encode(),
            )
            ddb_client.transact_write_items(TransactItems=[
                {'Delete': {
                    'TableName': os.environ['CHAT_TABLE'], 'Key': encoded(meta_key),
                    'ConditionExpression': '#members = :old AND #group = :true',
                    'ExpressionAttributeNames': {'#members': 'members', '#group': 'group'},
                    'ExpressionAttributeValues': encoded({':old': old_members, ':true': True}),
                }},
                {'Delete': {
                    'TableName': os.environ['CHAT_TABLE'],
                    'Key': encoded({'pk': f'USER#{user}', 'sk': f'CONV#{conversation}'}),
                    'ConditionExpression': 'attribute_exists(pk)',
                }},
            ])
            sockets = ddb.query(IndexName='ConnectionByUser', KeyConditionExpression=Key('userId').eq(user))['Items']
            for socket in sockets:
                if socket['pk'].startswith('CONN#'):
                    push(socket['pk'][5:], {'event': 'conversation', 'conversationId': conversation})
            return {'conversation': conversation, 'members': [], 'names': {}, 'deleted': True}
        new_members = [item for item in old_members if item != target]
        # Keep former members' names so their earlier messages remain readable.
        names = membership.get('names', {})
    timestamp = now()
    table = os.environ['CHAT_TABLE']
    tx = [{'Update': {
        'TableName': table, 'Key': encoded(meta_key),
        'UpdateExpression': 'SET #members = :new',
        'ConditionExpression': '#members = :old AND #group = :true',
        'ExpressionAttributeNames': {'#members': 'members', '#group': 'group'},
        'ExpressionAttributeValues': encoded({':new': new_members, ':old': old_members, ':true': True}),
    }}]
    for member_id in old_members:
        if member_id == target and not adding:
            continue
        tx.append({'Update': {
            'TableName': table,
            'Key': encoded({'pk': f'USER#{member_id}', 'sk': f'CONV#{conversation}'}),
            'UpdateExpression': 'SET #members = :members, #names = :names',
            'ConditionExpression': 'attribute_exists(pk)',
            'ExpressionAttributeNames': {'#members': 'members', '#names': 'names'},
            'ExpressionAttributeValues': encoded({':members': new_members, ':names': names}),
        }})
    target_key = encoded({'pk': f'USER#{target}', 'sk': f'CONV#{conversation}'})
    if adding:
        tx.append({'Put': {
            'TableName': table,
            'Item': encoded({'pk': f'USER#{target}', 'sk': f'CONV#{conversation}', 'id': conversation, 'members': new_members, 'group': True, 'title': meta['title'], 'names': names, 'updatedAt': timestamp}),
            'ConditionExpression': 'attribute_not_exists(pk)',
        }})
    else:
        tx.append({'Delete': {'TableName': table, 'Key': target_key, 'ConditionExpression': 'attribute_exists(pk)'}})
    ddb_client.transact_write_items(TransactItems=tx)
    for member_id in set([*old_members, *new_members]):
        sockets = ddb.query(IndexName='ConnectionByUser', KeyConditionExpression=Key('userId').eq(member_id))['Items']
        for socket in sockets:
            if socket['pk'].startswith('CONN#'):
                push(socket['pk'][5:], {'event': 'conversation', 'conversationId': conversation})
    return {'conversation': conversation, 'members': new_members, 'names': names}


def create_group_invite(user, body):
    conversation = str(body.get('conversation', ''))
    if not re.fullmatch(r'[a-f0-9]{32}', conversation) or not member(conversation, user):
        raise ValueError('Group chat not found.')
    meta = ddb.get_item(Key={'pk': f'CONV#{conversation}', 'sk': 'META'}, ConsistentRead=True).get('Item')
    if not meta or not meta.get('group') or user not in meta['members']:
        raise ValueError('Group chat not found.')
    token = uuid.uuid4().hex
    expires_at = int(time.time()) + 7 * 24 * 3600
    ddb.put_item(Item={'pk': f'INVITE#{token}', 'sk': 'META', 'conversationId': conversation, 'createdBy': user, 'ttl': expires_at}, ConditionExpression='attribute_not_exists(pk)')
    return {'token': token, 'expiresAt': expires_at}


def group_invite(user, body, accepting):
    token = body.get('token')
    if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
        raise ValueError('Invalid group invitation.')
    invite = ddb.get_item(Key={'pk': f'INVITE#{token}', 'sk': 'META'}, ConsistentRead=True).get('Item')
    if not invite or invite['ttl'] <= int(time.time()):
        raise ValueError('This group invitation has expired.')
    conversation = invite['conversationId']
    meta = ddb.get_item(Key={'pk': f'CONV#{conversation}', 'sk': 'META'}, ConsistentRead=True).get('Item')
    if not meta or not meta.get('group'):
        raise ValueError('This group is no longer available.')
    details = {'conversation': conversation, 'title': meta['title'], 'memberCount': len(meta['members']), 'alreadyMember': user in meta['members']}
    if not accepting or details['alreadyMember']:
        return details
    if len(meta['members']) >= 25:
        raise ValueError('This group is full.')
    name = body.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 40:
        raise ValueError('Choose a valid player name.')
    result = group_membership(meta['members'][0], {'conversation': conversation, 'userId': user, 'name': name.strip()}, True)
    return {**details, 'memberCount': len(result['members']), 'alreadyMember': True}


def create_conversation(user, body):
    other = body.get('members', [])
    if not isinstance(other, list) or not 1 <= len(other) <= 24 or any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', i) for i in other):
        raise ValueError('Choose 1 to 24 members.')
    members = sorted(set([user, *other]))
    if len(members) < 2:
        raise ValueError('Choose another user.')
    for member_id in members:
        if member_id != user:
            found = cognito.list_users(UserPoolId=os.environ['PLAYER_POOL_ID'], Filter=f'sub = "{member_id}"', Limit=1)
            if not found.get('Users'):
                raise ValueError('A selected player no longer exists.')
    is_group = bool(body.get('group')) or len(members) > 2
    title = str(body.get('title', '')).strip()[:80] if is_group else ''
    if is_group and not title:
        raise ValueError('Enter a group name.')
    conversation = uuid.uuid4().hex if is_group else 'dm-' + '-'.join(members)
    meta_key = {'pk': f'CONV#{conversation}', 'sk': 'META'}
    existing = ddb.get_item(Key=meta_key).get('Item')
    if existing:
        return {'conversation': conversation}
    names = body.get('names', {}) if isinstance(body.get('names'), dict) else {}
    timestamp = now()
    ddb.put_item(Item={**meta_key, 'members': members, 'group': is_group, 'title': title, 'createdAt': timestamp}, ConditionExpression='attribute_not_exists(pk)')
    for member_id in members:
        ddb.put_item(Item={'pk': f'USER#{member_id}', 'sk': f'CONV#{conversation}', 'id': conversation, 'members': members, 'group': is_group, 'title': title, 'names': {key: str(value)[:40] for key, value in names.items() if key in members}, 'updatedAt': timestamp})
        sockets = ddb.query(IndexName='ConnectionByUser', KeyConditionExpression=Key('userId').eq(member_id))['Items']
        for socket in sockets:
            if socket['pk'].startswith('CONN#'):
                push(socket['pk'][5:], {'event': 'conversation', 'conversationId': conversation})
    return {'conversation': conversation}


def history(user, body):
    conversation = str(body.get('conversation', ''))
    if not member(conversation, user):
        raise ValueError('Conversation not found.')
    cursor = body.get('cursor')
    if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r'MSG#\d{13}#[a-f0-9]{32}', cursor)):
        raise ValueError('Invalid message cursor.')
    query = {'KeyConditionExpression': Key('pk').eq(f'CONV#{conversation}') & Key('sk').begins_with('MSG#'), 'ScanIndexForward': False, 'Limit': 50}
    if cursor:
        query['ExclusiveStartKey'] = {'pk': f'CONV#{conversation}', 'sk': cursor}
    result = ddb.query(**query)
    return {'conversation': conversation, 'messages': [public_message(item) for item in reversed(result['Items'])], 'nextCursor': result.get('LastEvaluatedKey', {}).get('sk')}


def media_upload(user, body):
    kind = body.get('kind')
    content_type = body.get('contentType')
    size = body.get('size')
    allowed = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'} if kind == 'image' else {'video/mp4', 'video/webm', 'video/quicktime'} if kind == 'video' else set()
    if content_type not in allowed or type(size) is not int or not 0 < size <= 5 * 1024 * 1024:
        raise ValueError('Choose an image or video under 5 MB.')
    key = f'chat/{user}/{uuid.uuid4().hex}'
    url = s3.generate_presigned_url('put_object', Params={'Bucket': bucket, 'Key': key, 'ContentType': content_type}, ExpiresIn=300)
    return {'key': key, 'uploadUrl': url, 'contentType': content_type}


def send(user, body):
    conversation = str(body.get('conversation', ''))
    membership = member(conversation, user)
    if not membership:
        raise ValueError('Conversation not found.')
    kind = body.get('kind')
    text = str(body.get('text', '')).strip()
    key = body.get('key', '')
    if kind == 'text' and not 0 < len(text) <= 2000:
        raise ValueError('Enter a message up to 2000 characters.')
    mentions = body.get('mentions', [])
    if not isinstance(mentions, list):
        mentions = []
    valid_members = set(membership.get('members', [])) - {user} if membership.get('group') else set()
    mentions = list(dict.fromkeys(item for item in mentions if isinstance(item, str) and item in valid_members))[:25]
    if kind == 'gif':
        url = urlparse(text)
        if url.scheme != 'https' or url.hostname not in {'media.giphy.com', 'i.giphy.com', 'media.tenor.com', 'c.tenor.com'} or len(text) > 1000:
            raise ValueError('Choose a GIF from Giphy or Tenor.')
    if kind == 'sound' and text not in SOUND_IDS:
        raise ValueError('Choose a sound from the soundboard.')
    if kind in ('image', 'video'):
        if not isinstance(key, str) or not key.startswith(f'chat/{user}/'):
            raise ValueError('Invalid upload.')
        head = s3.head_object(Bucket=bucket, Key=key)
        allowed = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'} if kind == 'image' else {'video/mp4', 'video/webm', 'video/quicktime'}
        if head['ContentLength'] > 5 * 1024 * 1024 or head['ContentType'] not in allowed:
            raise ValueError('Choose an image or video under 5 MB.')
    if kind not in ('text', 'gif', 'image', 'video', 'sound'):
        raise ValueError('Invalid message type.')
    timestamp = now()
    item = {'pk': f'CONV#{conversation}', 'sk': f'MSG#{timestamp:013d}#{uuid.uuid4().hex}', 'id': uuid.uuid4().hex, 'conversationId': conversation, 'senderId': user, 'kind': kind, 'text': text if kind in ('text', 'gif', 'sound') else '', 'key': key if kind in ('image', 'video') else '', 'createdAt': timestamp}
    ddb_client.transact_write_items(TransactItems=[
        {'ConditionCheck': {
            'TableName': os.environ['CHAT_TABLE'],
            'Key': encoded({'pk': f'CONV#{conversation}', 'sk': 'META'}),
            'ConditionExpression': 'contains(#members, :user)',
            'ExpressionAttributeNames': {'#members': 'members'},
            'ExpressionAttributeValues': encoded({':user': user}),
        }},
        {'Put': {'TableName': os.environ['CHAT_TABLE'], 'Item': encoded(item)}},
    ])
    message = public_message(item)
    for member_id in membership['members']:
        try:
            ddb.update_item(Key={'pk': f'USER#{member_id}', 'sk': f'CONV#{conversation}'}, UpdateExpression='SET updatedAt=:t, lastMessage=:m', ConditionExpression='attribute_exists(pk)', ExpressionAttributeValues={':t': timestamp, ':m': text[:100] if kind in ('text', 'gif') else kind})
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
    for member_id in membership['members']:
        sockets = ddb.query(IndexName='ConnectionByUser', KeyConditionExpression=Key('userId').eq(member_id))['Items']
        for socket in sockets:
            if socket['pk'].startswith('CONN#'):
                push(socket['pk'][5:], {'event': 'message', 'message': message})
    recipients = [recipient for recipient in membership['members'] if recipient != user]
    fcm_configured = bool(os.environ.get('FCM_PROJECT_ID') and os.environ.get('FCM_SECRET_ARN'))
    if recipients and fcm_configured:
        names = membership.get('names', {})
        push_lambda.invoke(
            FunctionName=os.environ['PUSH_FUNCTION_NAME'], InvocationType='Event',
            Payload=json.dumps({
                'users': recipients,
                'conversation': conversation,
                'senderName': names.get(user, ''),
                'group': bool(membership.get('group')),
                'groupTitle': membership.get('title', ''),
                'kind': kind,
                'text': text if kind == 'text' else '',
                'mentions': mentions,
            }).encode(),
        )
        print(f'Queued push delivery for {len(recipients)} conversation member(s).')
    elif not recipients:
        print('Push delivery skipped: no other conversation members.')
    else:
        print('Push delivery skipped: Firebase project or secret ARN is not configured.')
    return {'message': message}


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


def react(user, body):
    conversation = str(body.get('conversation', ''))
    membership = member(conversation, user)
    if not membership:
        raise ValueError('Conversation not found.')
    message_key = body.get('messageKey')
    emoji = body.get('emoji')
    if not isinstance(message_key, str) or not re.fullmatch(r'MSG#\d{13}#[a-f0-9]{32}', message_key):
        raise ValueError('Invalid message.')
    if not isinstance(emoji, str) or emoji not in EMOJI:
        raise ValueError('Choose an emoji.')
    key = {'pk': f'CONV#{conversation}', 'sk': message_key}
    reaction_name = f'reaction_{user}'
    for _ in range(3):
        item = ddb.get_item(Key=key, ConsistentRead=True).get('Item')
        if not item:
            raise ValueError('Message not found.')
        previous = item.get(reaction_name)
        try:
            if previous == emoji:
                updated = ddb.update_item(Key=key, UpdateExpression='REMOVE #reaction', ConditionExpression='attribute_exists(pk) AND #reaction = :previous', ExpressionAttributeNames={'#reaction': reaction_name}, ExpressionAttributeValues={':previous': previous}, ReturnValues='ALL_NEW')['Attributes']
            else:
                condition = 'attribute_exists(pk) AND #reaction = :previous' if previous else 'attribute_exists(pk) AND attribute_not_exists(#reaction)'
                values = {':emoji': emoji, **({':previous': previous} if previous else {})}
                updated = ddb.update_item(Key=key, UpdateExpression='SET #reaction = :emoji', ConditionExpression=condition, ExpressionAttributeNames={'#reaction': reaction_name}, ExpressionAttributeValues=values, ReturnValues='ALL_NEW')['Attributes']
            reactions = message_reactions(updated)
            event = {'event': 'reaction', 'conversationId': conversation, 'messageKey': message_key, 'reactions': reactions}
            for member_id in membership['members']:
                sockets = ddb.query(IndexName='ConnectionByUser', KeyConditionExpression=Key('userId').eq(member_id))['Items']
                for socket in sockets:
                    if socket['pk'].startswith('CONN#'):
                        push(socket['pk'][5:], event)
            return {'messageKey': message_key, 'reactions': reactions}
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
    raise ValueError('Could not update reaction. Try again.')


def report(user, body):
    conversation = str(body.get('conversation', ''))
    if not member(conversation, user):
        raise ValueError('Conversation not found.')
    reason = str(body.get('reason', '')).strip()
    message_id = str(body.get('messageId', ''))
    if not 3 <= len(reason) <= 500:
        raise ValueError('Add a reason between 3 and 500 characters.')
    excerpt = {}
    if message_id:
        records = ddb.query(KeyConditionExpression=Key('pk').eq(f'CONV#{conversation}') & Key('sk').begins_with('MSG#'), ScanIndexForward=False, Limit=100)['Items']
        target = next((item for item in records if item['id'] == message_id), None)
        if not target:
            raise ValueError('Message not found.')
        excerpt = {'senderId': target['senderId'], 'messageKind': target['kind'], 'messageText': target.get('text', '')[:2000], 'mediaKey': target.get('key', '')}
    report_id = uuid.uuid4().hex
    ddb.put_item(Item={'pk': f'REPORT#{report_id}', 'sk': 'META', 'id': report_id, 'conversationId': conversation, 'messageId': message_id, 'reporterId': user, 'reason': reason, 'status': 'open', 'createdAt': now(), 'members': membership['members'], 'names': membership.get('names', {}), **excerpt})
    return {'reportId': report_id}


def cleanup_group(event, _context):
    """Remove the message history and media after the final member leaves."""
    conversation = event.get('conversation', '')
    if not isinstance(conversation, str) or not re.fullmatch(r'[a-f0-9]{32}', conversation):
        raise ValueError('Invalid group conversation.')
    partition = f'CONV#{conversation}'
    for _ in range(15):
        if not ddb.get_item(Key={'pk': partition, 'sk': 'META'}, ConsistentRead=True).get('Item'):
            break
        time.sleep(1)
    else:
        return {'deleted': False}
    while True:
        records = ddb.query(
            KeyConditionExpression=Key('pk').eq(partition) & Key('sk').begins_with('MSG#'),
            ConsistentRead=True, Limit=100,
        )['Items']
        if not records:
            break
        media = [{'Key': key} for key in {item.get('key') for item in records} if key and key.startswith('chat/')]
        if media:
            result = s3.delete_objects(Bucket=bucket, Delete={'Objects': media, 'Quiet': True})
            if result.get('Errors'):
                raise RuntimeError(f'Could not delete chat media: {result["Errors"]}')
        with ddb.batch_writer() as batch:
            for item in records:
                batch.delete_item(Key={'pk': partition, 'sk': item['sk']})
    return {'deleted': True}


def action(event, _context):
    connection = event['requestContext']['connectionId']
    try:
        body = json.loads(event.get('body') or '{}')
        if not isinstance(body, dict):
            raise ValueError('Invalid chat request.')
        request_id = body.get('requestId')
        route = event['requestContext']['routeKey']
        if route == 'authenticate':
            data = authenticate(connection, body)
        else:
            user = socket_user(connection)
            if route in ('createConversation', 'addGroupMember', 'createGroupInvite', 'acceptGroupInvite', 'mediaUpload', 'send', 'react') or (route == 'removeGroupMember' and body.get('userId') != user):
                ensure_chat_allowed(user)
            routes = {'conversations': lambda: conversations(user), 'createConversation': lambda: create_conversation(user, body), 'addGroupMember': lambda: group_membership(user, body, True), 'removeGroupMember': lambda: group_membership(user, body, False), 'createGroupInvite': lambda: create_group_invite(user, body), 'previewGroupInvite': lambda: group_invite(user, body, False), 'acceptGroupInvite': lambda: group_invite(user, body, True), 'history': lambda: history(user, body), 'mediaUpload': lambda: media_upload(user, body), 'send': lambda: send(user, body), 'react': lambda: react(user, body), 'setMute': lambda: set_conversation_mute(user, body), 'report': lambda: report(user, body), 'registerPush': lambda: register_push(user, body, True), 'unregisterPush': lambda: register_push(user, body, False)}
            if route not in routes:
                raise ValueError('Unknown chat action.')
            data = routes[route]()
        push(connection, {'event': 'response', 'requestId': request_id, 'data': data})
    except (ValueError, KeyError, ClientError) as error:
        push(connection, {'event': 'response', 'requestId': (body.get('requestId') if isinstance(locals().get('body'), dict) else None), 'error': str(error)})
    return ok()
