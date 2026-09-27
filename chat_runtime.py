"""Shared AWS clients and low-level chat utilities."""
import json
import os
import re
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config


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
with open(os.path.join(os.path.dirname(__file__), 'emoji-list.json'), encoding='utf-8') as emoji_file:
    EMOJI = frozenset(json.load(emoji_file))
SOUND_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
bucket = os.environ['CHAT_BUCKET']
cleanup_lambda = boto3.client('lambda')
push_lambda = boto3.client('lambda')
secrets = boto3.client('secretsmanager')
CONVERSATION_PAGE_SIZE = 25


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

def encoded(values):
    return {key: serializer.serialize(value) for key, value in values.items()}
