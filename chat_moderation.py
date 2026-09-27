"""Conversation reports and group data cleanup."""
import os
import re
import time
import uuid

from boto3.dynamodb.conditions import Key

from chat_runtime import bucket, ddb, member, now, s3

def report(user, body):
    conversation = str(body.get('conversation', ''))
    membership = member(conversation, user)
    if not membership:
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
