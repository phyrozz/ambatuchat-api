"""AWS Lambda entrypoints and WebSocket route wiring."""
import json

from botocore.exceptions import ClientError

from chat_auth import authenticate
from chat_conversations import conversations, create_conversation, set_conversation_mute
from chat_groups import create_group_invite, group_invite, group_membership
from chat_messages import history, mark_read, media_upload, send
from chat_moderation import cleanup_group as cleanup_group_data, report
from chat_notifications import (
    fcm_access_token,
    notification_preview,
    push_notifications as deliver_push_notifications,
    register_push,
    send_push_notifications,
)
from chat_reactions import react
from chat_runtime import (
    clean,
    ddb,
    encoded,
    ensure_chat_allowed,
    member,
    message_reactions,
    now,
    ok,
    public_message,
    push,
    socket_user,
)


def connect(_event, _context):
    return ok()


def disconnect(event, _context):
    connection = event['requestContext']['connectionId']
    ddb.delete_item(Key={'pk': f'CONN#{connection}', 'sk': 'META'})
    return ok()


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
            restricted_routes = {
                'createConversation', 'addGroupMember', 'createGroupInvite',
                'acceptGroupInvite', 'mediaUpload', 'send', 'react',
            }
            if route in restricted_routes or (route == 'removeGroupMember' and body.get('userId') != user):
                ensure_chat_allowed(user)
            routes = {
                'conversations': lambda: conversations(user, body),
                'createConversation': lambda: create_conversation(user, body),
                'addGroupMember': lambda: group_membership(user, body, True),
                'removeGroupMember': lambda: group_membership(user, body, False),
                'createGroupInvite': lambda: create_group_invite(user, body),
                'previewGroupInvite': lambda: group_invite(user, body, False),
                'acceptGroupInvite': lambda: group_invite(user, body, True),
                'history': lambda: history(user, body),
                'markRead': lambda: mark_read(user, body),
                'mediaUpload': lambda: media_upload(user, body),
                'send': lambda: send(user, body),
                'react': lambda: react(user, body),
                'setMute': lambda: set_conversation_mute(user, body),
                'report': lambda: report(user, body),
                'registerPush': lambda: register_push(user, body, True),
                'unregisterPush': lambda: register_push(user, body, False),
            }
            if route not in routes:
                raise ValueError('Unknown chat action.')
            data = routes[route]()
        push(connection, {'event': 'response', 'requestId': request_id, 'data': data})
    except (ValueError, KeyError, ClientError) as error:
        push(connection, {
            'event': 'response',
            'requestId': body.get('requestId') if isinstance(locals().get('body'), dict) else None,
            'error': str(error),
        })
    return ok()


def cleanup_group(event, context):
    return cleanup_group_data(event, context)


def push_notifications(event, context):
    return deliver_push_notifications(event, context)
