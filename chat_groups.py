"""Group membership and invitation operations."""
import json
import os
import re
import time
import uuid

from boto3.dynamodb.conditions import Key

from chat_runtime import cleanup_lambda, cognito, ddb, ddb_client, encoded, member, now, push

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
            'Item': encoded({'pk': f'USER#{target}', 'sk': f'CONV#{conversation}', 'id': conversation, 'members': new_members, 'group': True, 'title': meta['title'], 'names': names, 'updatedAt': timestamp, 'unreadCount': 0}),
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
