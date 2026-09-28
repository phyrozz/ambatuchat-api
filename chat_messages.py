"""Message history, uploads, reads, and sends."""
import json
import os
import re
import uuid
from urllib.parse import urlparse

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from chat_runtime import (
    SOUND_ID_PATTERN, bucket, ddb, ddb_client, encoded, member, now,
    public_message, push, push_lambda, s3,
)

def history(user, body):
    conversation = str(body.get('conversation', ''))
    if not member(conversation, user):
        raise ValueError('Conversation not found.')
    cursor = body.get('cursor')
    after = body.get('after')
    if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r'MSG#\d{13}#[a-f0-9]{32}', cursor)):
        raise ValueError('Invalid message cursor.')
    if after is not None and (not isinstance(after, str) or not re.fullmatch(r'MSG#\d{13}#[a-f0-9]{32}', after)):
        raise ValueError('Invalid message position.')
    if cursor and after:
        raise ValueError('Choose either an older or newer message position.')

    refresh_media = body.get('refreshMedia', [])
    if not isinstance(refresh_media, list) or len(refresh_media) > 100 or any(
        not isinstance(key, str) or not re.fullmatch(r'MSG#\d{13}#[a-f0-9]{32}', key)
        for key in refresh_media
    ):
        raise ValueError('Invalid media refresh request.')

    query = None
    if after:
        query = {
            'KeyConditionExpression': Key('pk').eq(f'CONV#{conversation}') & Key('sk').gt(after),
            'ScanIndexForward': True,
            'Limit': 50,
        }
    elif cursor:
        query = {
            'KeyConditionExpression': Key('pk').eq(f'CONV#{conversation}') & Key('sk').lt(cursor),
            'ScanIndexForward': False,
            'Limit': 50,
        }
    elif not refresh_media:
        query = {'KeyConditionExpression': Key('pk').eq(f'CONV#{conversation}') & Key('sk').begins_with('MSG#'), 'ScanIndexForward': False, 'Limit': 50}
    result = ddb.query(**query) if query else {'Items': []}
    messages = [public_message(item) for item in result['Items']]
    if not after:
        messages.reverse()
    response = {
        'conversation': conversation,
        'messages': messages,
        'nextCursor': result.get('LastEvaluatedKey', {}).get('sk') if not after else None,
    }
    if after:
        response['newerCursor'] = result.get('LastEvaluatedKey', {}).get('sk')
    if refresh_media:
        response['mediaUrls'] = {
            key: public_message(item)['url']
            for key in refresh_media
            if (item := ddb.get_item(Key={'pk': f'CONV#{conversation}', 'sk': key}).get('Item'))
            and item.get('kind') in ('image', 'video')
            and item.get('key')
        }
    return response

def mark_read(user, body):
    conversation = str(body.get('conversation', ''))
    if not member(conversation, user):
        raise ValueError('Conversation not found.')
    seen_through = body.get('seenThrough')
    if not isinstance(seen_through, str) or not re.fullmatch(r'MSG#\d{13}#[a-f0-9]{32}', seen_through):
        raise ValueError('Invalid read position.')
    if not ddb.get_item(Key={'pk': f'CONV#{conversation}', 'sk': seen_through}, ConsistentRead=True).get('Item'):
        raise ValueError('Message not found.')
    key = {'pk': f'USER#{user}', 'sk': f'CONV#{conversation}'}
    try:
        result = ddb.update_item(
            Key=key,
            UpdateExpression='SET unreadCount = :zero REMOVE lastUnreadMessageKey',
            ConditionExpression='attribute_exists(pk) AND (attribute_not_exists(lastUnreadMessageKey) OR lastUnreadMessageKey <= :seen)',
            ExpressionAttributeValues={':zero': 0, ':seen': seen_through},
            ReturnValues='ALL_NEW',
        )
        return {'conversation': conversation, 'read': True, 'unreadCount': int(result['Attributes'].get('unreadCount', 0))}
    except ClientError as error:
        if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        current = ddb.get_item(Key=key, ConsistentRead=True).get('Item', {})
        return {'conversation': conversation, 'read': False, 'unreadCount': int(current.get('unreadCount', 0))}

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
    if kind == 'sound' and (not isinstance(text, str) or not SOUND_ID_PATTERN.fullmatch(text)):
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
    transaction = [
        {'ConditionCheck': {
            'TableName': os.environ['CHAT_TABLE'],
            'Key': encoded({'pk': f'CONV#{conversation}', 'sk': 'META'}),
            'ConditionExpression': '#members = :members',
            'ExpressionAttributeNames': {'#members': 'members'},
            'ExpressionAttributeValues': encoded({':members': membership['members']}),
        }},
        {'Put': {'TableName': os.environ['CHAT_TABLE'], 'Item': encoded(item)}},
    ]
    message = public_message(item)
    for member_id in membership['members']:
        update_expression = 'SET updatedAt=:t, lastMessage=:m, unreadCount=:zero REMOVE lastUnreadMessageKey' if member_id == user else 'SET updatedAt=:t, lastMessage=:m, lastUnreadMessageKey=:messageKey ADD unreadCount :one'
        values = {':t': timestamp, ':m': text[:100] if kind in ('text', 'gif') else kind, ':zero': 0} if member_id == user else {':t': timestamp, ':m': text[:100] if kind in ('text', 'gif') else kind, ':messageKey': item['sk'], ':one': 1}
        transaction.append({'Update': {
            'TableName': os.environ['CHAT_TABLE'],
            'Key': encoded({'pk': f'USER#{member_id}', 'sk': f'CONV#{conversation}'}),
            'UpdateExpression': update_expression,
            'ConditionExpression': 'attribute_exists(pk)',
            'ExpressionAttributeValues': encoded(values),
        }})
    ddb_client.transact_write_items(TransactItems=transaction)
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
