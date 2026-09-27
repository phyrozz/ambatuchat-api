"""Cognito authentication for WebSocket sessions."""
import base64
import json
import os
import time

from chat_runtime import cognito, ddb

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
