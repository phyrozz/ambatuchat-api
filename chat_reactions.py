"""Message reaction operations."""
import re

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from chat_runtime import EMOJI, ddb, member, message_reactions, push

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
