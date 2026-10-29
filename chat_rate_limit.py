"""Atomic per-account limits shared by all WebSocket connections."""
import time

from botocore.exceptions import ClientError

from chat_runtime import ddb


# Limits are per fixed window; bursts across a window boundary can reach twice
# the stated count. Other authenticated routes share the default limit.
LIMITS = {
    'send': (30, 60),
    'mediaUpload': (10, 600),
    'history': (60, 60),
    'createConversation': (20, 600),
    'addGroupMember': (30, 600),
    'createGroupInvite': (30, 600),
    'acceptGroupInvite': (30, 600),
    'registerPush': (20, 600),
}


def check_rate_limit(user, route):
    limit, seconds = LIMITS.get(route, (120, 60))
    timestamp = int(time.time())
    window = timestamp // seconds
    try:
        ddb.update_item(
            Key={'pk': f'RATE#{user}', 'sk': f'{route}#{window}'},
            UpdateExpression='SET #count = if_not_exists(#count, :zero) + :one, #ttl = :ttl',
            ConditionExpression='attribute_not_exists(#count) OR #count < :limit',
            ExpressionAttributeNames={'#count': 'count', '#ttl': 'ttl'},
            ExpressionAttributeValues={':zero': 0, ':one': 1, ':limit': limit, ':ttl': (window + 1) * seconds + 86400},
        )
    except ClientError as error:
        if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise ValueError('RATE_LIMITED') from None
        raise
