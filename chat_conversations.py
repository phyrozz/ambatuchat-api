"""Conversation inbox, creation, and mute operations."""
import os
import re
import uuid

from boto3.dynamodb.conditions import Key

from chat_runtime import CONVERSATION_PAGE_SIZE, cognito, clean, ddb, member, now, push

def conversations(user, body):
    query = {
        'IndexName': 'ConversationsByUserUpdatedAt',
        'KeyConditionExpression': Key('pk').eq(f'USER#{user}'),
        'ScanIndexForward': False,
        'Limit': CONVERSATION_PAGE_SIZE,
    }
    cursor = body.get('cursor')
    if cursor is not None:
        if (
            not isinstance(cursor, dict)
            or cursor.get('pk') != f'USER#{user}'
            or not isinstance(cursor.get('sk'), str)
            or not cursor['sk'].startswith('CONV#')
            or type(cursor.get('updatedAt')) is not int
        ):
            raise ValueError('Invalid conversation cursor.')
        query['ExclusiveStartKey'] = {key: cursor[key] for key in ('pk', 'sk', 'updatedAt')}
    result = ddb.query(**query)
    response = {
        'conversations': [clean(item) for item in result['Items']],
        'nextCursor': clean(result.get('LastEvaluatedKey')),
    }
    active_id = body.get('activeConversation')
    if active_id is not None:
        if not isinstance(active_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,2000}', active_id):
            raise ValueError('Invalid active conversation.')
        response['activeConversation'] = clean(member(active_id, user))
    return response

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
        ddb.put_item(Item={'pk': f'USER#{member_id}', 'sk': f'CONV#{conversation}', 'id': conversation, 'members': members, 'group': is_group, 'title': title, 'names': {key: str(value)[:40] for key, value in names.items() if key in members}, 'updatedAt': timestamp, 'unreadCount': 0})
        sockets = ddb.query(IndexName='ConnectionByUser', KeyConditionExpression=Key('userId').eq(member_id))['Items']
        for socket in sockets:
            if socket['pk'].startswith('CONN#'):
                push(socket['pk'][5:], {'event': 'conversation', 'conversationId': conversation})
    return {'conversation': conversation}
