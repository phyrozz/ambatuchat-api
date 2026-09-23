"""API Gateway WebSocket handlers. Every data route requires a Cognito-authenticated socket."""
import base64
import json
import os
import re
import time
import uuid
from decimal import Decimal
from urllib.parse import urlparse

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

ddb = boto3.resource('dynamodb').Table(os.environ['CHAT_TABLE'])
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
bucket = os.environ['CHAT_BUCKET']


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
    return ddb.get_item(Key={'pk': f'USER#{user}', 'sk': f'CONV#{conversation}'}).get('Item')


def ensure_chat_allowed(user):
    restriction = ddb.get_item(Key={'pk': f'CHAT_RESTRICTION#{user}', 'sk': 'META'}).get('Item')
    if restriction and restriction.get('restricted') is True:
        raise ValueError('CHAT_RESTRICTED')


def public_message(item):
    output = {key: item[key] for key in ('id', 'conversationId', 'senderId', 'kind', 'text', 'key', 'createdAt') if key in item}
    if output.get('key'):
        output['url'] = s3.generate_presigned_url('get_object', Params={'Bucket': bucket, 'Key': output['key']}, ExpiresIn=3600)
    return output


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
    result = ddb.query(KeyConditionExpression=Key('pk').eq(f'USER#{user}') & Key('sk').begins_with('CONV#'))
    return {'conversations': [clean(item) for item in result['Items']]}


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
    result = ddb.query(KeyConditionExpression=Key('pk').eq(f'CONV#{conversation}') & Key('sk').begins_with('MSG#'), ScanIndexForward=False, Limit=50)
    return {'conversation': conversation, 'messages': [public_message(item) for item in reversed(result['Items'])]}


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
    ddb.put_item(Item=item)
    message = public_message(item)
    for member_id in membership['members']:
        ddb.update_item(Key={'pk': f'USER#{member_id}', 'sk': f'CONV#{conversation}'}, UpdateExpression='SET updatedAt=:t, lastMessage=:m', ExpressionAttributeValues={':t': timestamp, ':m': text[:100] if kind in ('text', 'gif') else kind})
    for member_id in membership['members']:
        sockets = ddb.query(IndexName='ConnectionByUser', KeyConditionExpression=Key('userId').eq(member_id))['Items']
        for socket in sockets:
            if socket['pk'].startswith('CONN#'):
                push(socket['pk'][5:], {'event': 'message', 'message': message})
    return {'message': message}


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
            if route in ('createConversation', 'mediaUpload', 'send'):
                ensure_chat_allowed(user)
            routes = {'conversations': lambda: conversations(user), 'createConversation': lambda: create_conversation(user, body), 'history': lambda: history(user, body), 'mediaUpload': lambda: media_upload(user, body), 'send': lambda: send(user, body), 'report': lambda: report(user, body)}
            if route not in routes:
                raise ValueError('Unknown chat action.')
            data = routes[route]()
        push(connection, {'event': 'response', 'requestId': request_id, 'data': data})
    except (ValueError, KeyError, ClientError) as error:
        push(connection, {'event': 'response', 'requestId': (body.get('requestId') if isinstance(locals().get('body'), dict) else None), 'error': str(error)})
    return ok()
