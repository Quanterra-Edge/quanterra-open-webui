"""Conversation continuity for Quanterra hosted runtimes on a ``responses`` connection.

Upstream Open WebUI replays the whole chat on every turn, including the
``function_call`` / ``function_call_output`` items of earlier turns
(``routers/openai.py`` ``convert_to_responses_payload``). The Quanterra hosted
runtime's official parser accepts ``message`` items only, so a chat whose history
holds one server-side tool round fails with 400 on every later turn.

For a Quanterra connection (connection tags carry ``quanterra``) the main chat
turn therefore sends only the new user message plus a ``conversation_id`` that
the runtime keeps the history under, one conversation per (chat, model), stored
in the chat's ``meta``. Background task calls (title, tags, follow-ups, ...)
stay stateless: their messages go as plain ``message`` items. Every other
provider is left untouched.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

OWNER_TAG = 'quanterra'
META_KEY = 'quanterra'
STALE_CONVERSATION_STATUSES = (409, 410)
CREATE_TIMEOUT_SECONDS = 10
# The runtime keeps the history; these never ride along on a Quanterra body.
DROPPED_KEYS = ('tools', 'tool_choice', 'conversation_id', 'previous_response_id')


# ------------------------------------------------------------------ pure parts


def is_quanterra_connection(config: dict[str, Any] | None) -> bool:
    """True for a connection config, or a model dict carrying its tags, tagged ``quanterra``."""
    tags = (config or {}).get('tags') or []
    return any((tag.get('name') if isinstance(tag, dict) else tag) == OWNER_TAG for tag in tags)


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


def retry_body(status: int, fallback: dict[str, Any] | None, conversation_id: str | None) -> dict[str, Any] | None:
    """What to re-send after the chat call answered ``status``.

    None: nothing to retry (not a Quanterra chat turn, or the status is not a stale
    conversation). With a fresh ``conversation_id`` the turn goes on that
    conversation; without one the stateless ``fallback`` keeps the chat working.
    """
    if fallback is None or status not in STALE_CONVERSATION_STATUSES:
        return None
    message = last_user_message(fallback.get('input'))
    if conversation_id and message:
        return continuity_payload(fallback, message, conversation_id)
    return fallback


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


async def load_meta(chat_id: str) -> dict[str, Any] | None:
    """The chat's meta, or None when the chat is not saved (temporary chat, channel, unknown id)."""
    from open_webui.models.chats import Chats
    from open_webui.utils.chat_id import is_saved_chat_id

    if not is_saved_chat_id(chat_id):
        return None
    chat = await Chats.get_chat_by_id(chat_id)
    return dict(chat.meta or {}) if chat else None


async def save_meta(chat_id: str, meta: dict[str, Any]) -> None:
    # ponytail: read-modify-write of the meta JSON like update_chat_tags_by_id; a tags write racing this one
    # can drop the other's key, which only costs a fresh conversation (or a tag) next turn.
    from sqlalchemy import update

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat

    async with get_async_db_context() as session:
        await session.execute(update(Chat).filter_by(id=chat_id).values(meta=meta))
        await session.commit()


async def start_conversation(chat_id: str, model_id: str, url: str, headers: dict[str, str]) -> str | None:
    """Create a runtime conversation for (chat, model) and record it in the chat meta."""
    meta = await load_meta(chat_id)
    if meta is None:
        return None
    conversation_id = await create_conversation(url, headers)
    if conversation_id:
        await save_meta(chat_id, remember_conversation(meta, model_id, conversation_id))
    return conversation_id


async def prepare(
    payload: dict[str, Any],
    api_config: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    headers: dict[str, str],
    url: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Shape the converted Responses body for a Quanterra connection.

    Returns ``(body, fallback)``. ``fallback`` is the stateless body to re-send when
    the runtime reports the stored conversation stale; it is None for task calls,
    for turns that cannot ride a conversation and for every other provider.
    """
    if not is_quanterra_connection(api_config):
        return payload, None
    stateless = stateless_payload(payload)
    message = last_user_message(payload.get('input'))
    chat_id = (metadata or {}).get('chat_id')
    if is_task_call(metadata) or message is None or not chat_id:
        return stateless, None
    model_id = str(payload.get('model') or '')
    conversation_id = conversation_for(model_id, await load_meta(chat_id)) or await start_conversation(
        chat_id, model_id, url, headers
    )
    if not conversation_id:
        return stateless, None
    return continuity_payload(payload, message, conversation_id), stateless


async def retry(
    response: Any,
    fallback: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    headers: dict[str, str],
    url: str,
) -> dict[str, Any] | None:
    """After the chat call: a stale conversation (409/410) is replaced once; the body to re-send, or None."""
    if fallback is None or response.status not in STALE_CONVERSATION_STATUSES:
        return None
    response.release()
    chat_id = str((metadata or {}).get('chat_id') or '')
    conversation_id = await start_conversation(chat_id, str(fallback.get('model') or ''), url, headers)
    return retry_body(response.status, fallback, conversation_id)
