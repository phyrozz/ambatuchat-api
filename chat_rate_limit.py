"""Atomic per-account limits shared by all WebSocket connections."""
import time
from decimal import Decimal

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
DEFAULT_LIMIT = (120, 60)


def check_rate_limit(user, route):
    configured = ddb.get_item(Key={'pk': 'SETTINGS#chat', 'sk': 'RATE_LIMITS'}, ConsistentRead=True).get('Item', {}).get('limits', {})
    default = LIMITS.get(route, DEFAULT_LIMIT)
    settings = configured.get(route if route in LIMITS else 'default', {}) if isinstance(configured, dict) else {}
    if not isinstance(settings, dict):
        settings = {}
    limit, seconds = settings.get('limit'), settings.get('seconds')
    if not (isinstance(limit, (int, Decimal)) and not isinstance(limit, bool) and limit % 1 == 0 and 1 <= limit <= 10000
            and isinstance(seconds, (int, Decimal)) and not isinstance(seconds, bool) and seconds % 1 == 0 and 10 <= seconds <= 86400):
        limit, seconds = default
    limit, seconds = int(limit), int(seconds)
    timestamp = int(time.time())
    window = timestamp // seconds
    try:
        ddb.update_item(
            Key={'pk': f'RATE#{user}', 'sk': f'{route}#{seconds}#{window}'},
            UpdateExpression='SET #count = if_not_exists(#count, :zero) + :one, #ttl = :ttl',
            ConditionExpression='attribute_not_exists(#count) OR #count < :limit',
            ExpressionAttributeNames={'#count': 'count', '#ttl': 'ttl'},
            ExpressionAttributeValues={':zero': 0, ':one': 1, ':limit': limit, ':ttl': (window + 1) * seconds + 86400},
        )
    except ClientError as error:
        if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise ValueError('RATE_LIMITED') from None
        raise
