"""Conversation continuity for Quanterra hosted runtimes on a ``responses`` connection.

Upstream Open WebUI replays the whole chat on every turn, including the
``function_call`` / ``function_call_output`` items of earlier turns
(``routers/openai.py`` ``convert_to_responses_payload``). The Quanterra hosted
runtime's official parser accepts ``message`` items only, so a chat whose history
holds one server-side tool round fails with 400 on every later turn.

For a Quanterra connection (connection tags carry ``quanterra``) the chat turn
therefore rides a runtime ``conversation_id``, one per (chat, model), stored in
the chat's ``meta``. The first turn on a conversation seeds it with the chat's
history as plain ``message`` items; every later turn sends only the new user
message. Background task calls (title, tags, follow-ups, ...) stay stateless:
their messages go as plain ``message`` items. Every other provider is left
untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

log = logging.getLogger(__name__)

OWNER_TAG = 'quanterra'
META_KEY = 'quanterra'
CONFLICT_STATUSES = (409, 410)
# The runtime answers 409 "unknown continuation id for this caller" for a conversation it does not
# know; its other 409s (active writer, idempotency replay) are transient and keep the conversation.
UNKNOWN_CONVERSATION = 'unknown'
CREATE_TIMEOUT_SECONDS = 10
# The runtime keeps the history; these never ride along on a Quanterra body.
DROPPED_KEYS = ('tools', 'tool_choice', 'conversation_id', 'previous_response_id')
# Shown in the chat instead of the runtime's bare 401 "missing bearer token".
SIGN_IN_AGAIN = (
    'Your Quanterra sign-in has expired. Sign out of Open WebUI and sign in again to keep chatting with this agent.'
)


# ------------------------------------------------------------------ pure parts


def is_quanterra_connection(config: dict[str, Any] | None) -> bool:
    """True for a connection config, or a model dict carrying its tags, tagged ``quanterra``."""
    tags = (config or {}).get('tags') or []
    return any((tag.get('name') if isinstance(tag, dict) else tag) == OWNER_TAG for tag in tags)


def is_quanterra_model(model: dict[str, Any] | None, models: dict[str, Any] | None) -> bool:
    """A model served by a Quanterra connection: tagged itself, or a workspace preset whose base model is."""
    base_id = ((model or {}).get('info') or {}).get('base_model_id') or ''
    return is_quanterra_connection(model) or is_quanterra_connection((models or {}).get(base_id))


def is_task_call(metadata: dict[str, Any] | None) -> bool:
    """Title, tags, follow-ups, memory, query generation ... set ``metadata.task``; the chat turn does not."""
    return bool((metadata or {}).get('task'))


def strip_history(input_items: Any) -> list[dict[str, Any]]:
    """Keep the ``message`` items only; the runtime's parser rejects every other item type."""
    if not isinstance(input_items, list):
        return []
    return [item for item in input_items if isinstance(item, dict) and item.get('type') == 'message']


def last_user_message(input_items: Any) -> dict[str, Any] | None:
    """The new user message when the turn ends with one; None for Continue and tool follow-ups."""
    if not isinstance(input_items, list) or not input_items:
        return None
    last = input_items[-1]
    if isinstance(last, dict) and last.get('type') == 'message' and last.get('role') == 'user':
        return last
    return None


def stateless_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The history as plain messages and no server-side state: task calls and the fallback body."""
    body = {key: value for key, value in payload.items() if key not in DROPPED_KEYS}
    body['input'] = strip_history(payload.get('input'))
    return body


def continuity_payload(payload: dict[str, Any], message: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    """Only the new user message rides on the runtime conversation; instructions, model and stream stay."""
    body = stateless_payload(payload)
    body['input'] = [message]
    body['conversation_id'] = conversation_id
    return body


def seeded_payload(stateless: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    """A fresh conversation starts empty: the whole history as messages rides on its first turn."""
    return {**stateless, 'conversation_id': conversation_id}


def conversation_for(model_id: str, meta: dict[str, Any] | None) -> str | None:
    conversations = ((meta or {}).get(META_KEY) or {}).get('conversations') or {}
    value = conversations.get(model_id)
    return value if isinstance(value, str) and value else None


def remember_conversation(meta: dict[str, Any] | None, model_id: str, conversation_id: str) -> dict[str, Any]:
    """A new meta dict with the conversation recorded; every other meta key (tags, ...) is kept."""
    meta = dict(meta or {})
    quanterra = dict(meta.get(META_KEY) or {})
    quanterra['conversations'] = {**(quanterra.get('conversations') or {}), model_id: conversation_id}
    meta[META_KEY] = quanterra
    return meta


def conversation_gone(status: int, error: str) -> bool:
    """410, or the runtime's 409 for a conversation it does not know; every other 409 is transient."""
    return status == 410 or (status == 409 and UNKNOWN_CONVERSATION in (error or ''))


# ----------------------------------------------------------------- async parts


async def create_conversation(url: str, headers: dict[str, str]) -> str | None:
    """``POST <responses url>/conversations`` with the chat call's headers (same bearer, same thread header)."""
    import aiohttp

    from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
    from open_webui.utils.session_pool import get_session

    try:
        session = await get_session()
        async with session.post(
            f'{url.rstrip("/")}/conversations',
            data='{}',
            headers=headers,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=aiohttp.ClientTimeout(total=CREATE_TIMEOUT_SECONDS),
        ) as response:
            if response.status >= 400:
                log.warning('quanterra conversation creation failed: HTTP %d', response.status)
                return None
            data = await response.json()
    except Exception as exc:  # noqa: BLE001 - the chat falls back to the stateless body
        log.warning('quanterra conversation creation failed: %s', exc)
        return None
    conversation_id = data.get('id') if isinstance(data, dict) else None
    return conversation_id if isinstance(conversation_id, str) and conversation_id else None


async def load_meta(chat_id: str, user_id: str) -> dict[str, Any] | None:
    """The chat's meta when the chat is saved and the caller's; None otherwise (temporary chat, channel, other user)."""
    from sqlalchemy import select

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat
    from open_webui.utils.chat_id import is_saved_chat_id

    if not is_saved_chat_id(chat_id):
        return None
    async with get_async_db_context() as session:
        row = (await session.execute(select(Chat.meta).filter_by(id=chat_id, user_id=user_id))).one_or_none()
    return dict(row[0] or {}) if row else None


async def save_meta(chat_id: str, user_id: str, meta: dict[str, Any]) -> None:
    # ponytail: read-modify-write of the meta JSON like update_chat_tags_by_id; a tags write racing this one
    # can drop the other's key, which only costs a fresh conversation (or a tag) next turn.
    from sqlalchemy import update

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat

    async with get_async_db_context() as session:
        await session.execute(update(Chat).filter_by(id=chat_id, user_id=user_id).values(meta=meta))
        await session.commit()


async def start_conversation(
    chat_id: str, user_id: str, model_id: str, meta: dict[str, Any], url: str, headers: dict[str, str]
) -> str | None:
    """Create a runtime conversation for (chat, model) and record it in the chat meta."""
    conversation_id = await create_conversation(url, headers)
    if conversation_id:
        await save_meta(chat_id, user_id, remember_conversation(meta, model_id, conversation_id))
    return conversation_id


async def prepare(
    payload: dict[str, Any],
    api_config: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    user_id: str,
    headers: dict[str, str],
    url: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Shape the converted Responses body for a Quanterra connection.

    Returns ``(body, fallback)``. ``fallback`` is the stateless body to re-send when
    the runtime reports a conflict on the conversation; it is None for task calls,
    for turns that cannot ride a conversation and for every other provider.
    """
    if not is_quanterra_connection(api_config) or (api_config or {}).get('api_type') != 'responses':
        return payload, None
    # ponytail: discovery itself needs the caller's Keycloak token, so a Quanterra connection
    # with no bearer means the OAuth session is gone (its refresh failed and upstream deleted it).
    if 'Authorization' not in headers:
        raise HTTPException(status_code=401, detail=SIGN_IN_AGAIN)
    stateless = stateless_payload(payload)
    message = last_user_message(payload.get('input'))
    chat_id = str((metadata or {}).get('chat_id') or '')
    if is_task_call(metadata) or message is None or not chat_id:
        return stateless, None
    meta = await load_meta(chat_id, user_id)
    if meta is None:
        return stateless, None
    model_id = str(payload.get('model') or '')
    if conversation_id := conversation_for(model_id, meta):
        return continuity_payload(payload, message, conversation_id), stateless
    conversation_id = await start_conversation(chat_id, user_id, model_id, meta, url, headers)
    if not conversation_id:
        return stateless, None
    return seeded_payload(stateless, conversation_id), stateless


async def retry(
    response: Any,
    fallback: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    user_id: str,
    headers: dict[str, str],
    url: str,
) -> dict[str, Any] | None:
    """After the chat call answered a conflict: the body to re-send, or None.

    A transient 409 (active writer, idempotency replay) re-sends the history
    stateless and keeps the stored conversation. A conversation the runtime does
    not know (409 unknown, 410) is replaced once and the history re-sent on the
    new one; when creation fails the stateless history goes out instead.
    """
    if fallback is None or response.status not in CONFLICT_STATUSES:
        return None
    error = await response.text()
    response.release()
    if not conversation_gone(response.status, error):
        return fallback
    chat_id = str((metadata or {}).get('chat_id') or '')
    meta = await load_meta(chat_id, user_id)
    if meta is None:
        return fallback
    conversation_id = await start_conversation(chat_id, user_id, str(fallback.get('model') or ''), meta, url, headers)
    return seeded_payload(fallback, conversation_id) if conversation_id else fallback
