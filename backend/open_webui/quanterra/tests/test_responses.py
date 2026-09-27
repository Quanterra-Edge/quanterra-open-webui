"""Conversation continuity: what a Quanterra runtime receives per turn, and the hook's decisions."""

import asyncio
from types import SimpleNamespace

from open_webui.quanterra import responses
from open_webui.quanterra.responses import (
    continuity_payload,
    conversation_for,
    is_quanterra_connection,
    is_task_call,
    last_user_message,
    prepare,
    remember_conversation,
    retry,
    retry_body,
    stateless_payload,
    strip_history,
)

QUANTERRA = {'api_type': 'responses', 'tags': [{'name': 'quanterra'}, {'name': 'quanterra:rt-1'}]}
HEADERS = {'Authorization': 'Bearer t', 'x-quenterra-thread-id': 'chat-1'}
URL = 'http://runtime:8088/responses'


class FakeStore:
    """Stands in for the chat meta (DB) and the runtime's conversation endpoint."""

    def __init__(self, meta=None, create=('conv_1', 'conv_2')):
        self.meta = meta
        self.created = list(create)
        self.create_calls = []

    def install(self, monkeypatch):
        async def load_meta(chat_id):
            return None if self.meta is None else dict(self.meta)

        async def save_meta(chat_id, meta):
            self.meta = meta

        async def create_conversation(url, headers):
            self.create_calls.append((url, headers))
            return self.created.pop(0) if self.created else None

        monkeypatch.setattr(responses, 'load_meta', load_meta)
        monkeypatch.setattr(responses, 'save_meta', save_meta)
        monkeypatch.setattr(responses, 'create_conversation', create_conversation)


def _message(role: str, text: str) -> dict:
    text_type = 'output_text' if role == 'assistant' else 'input_text'
    return {'type': 'message', 'role': role, 'content': [{'type': text_type, 'text': text}]}


# What convert_to_responses_payload builds for the second turn of a chat whose first
# turn ran a server-side tool: the stored output items are replayed as history.
SECOND_TURN = {
    'model': 'OWUI_Test_Skills',
    'stream': True,
    'instructions': 'You are helpful.',
    'tools': [{'type': 'function', 'name': 'search', 'parameters': {}}],
    'tool_choice': 'auto',
    'input': [
        _message('user', 'build an xlsx'),
        {'type': 'reasoning', 'id': 'rs_1', 'summary': []},
        {'type': 'function_call', 'call_id': 'call_1', 'name': 'run_skill_script', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'ok'},
        _message('assistant', 'Done: /mnt/data/owui-test.xlsx'),
        _message('user', 'now a docx'),
    ],
}


def test_only_connections_tagged_quanterra_count():
    assert is_quanterra_connection({'tags': [{'name': 'quanterra'}, {'name': 'quanterra:rt-1'}]})
    assert is_quanterra_connection({'tags': ['quanterra']})
    assert not is_quanterra_connection({'tags': [{'name': 'openai'}]})
    assert not is_quanterra_connection({'api_type': 'responses'})
    assert not is_quanterra_connection(None)


def test_task_calls_are_told_apart_from_the_chat_turn():
    assert is_task_call({'task': 'title_generation', 'chat_id': 'c1'})
    assert not is_task_call({'chat_id': 'c1'})
    assert not is_task_call(None)


def test_history_keeps_message_items_only():
    kept = strip_history(SECOND_TURN['input'])
    assert [item['type'] for item in kept] == ['message', 'message', 'message']
    assert [item['role'] for item in kept] == ['user', 'assistant', 'user']
    assert strip_history('plain string') == []


def test_second_turn_after_a_tool_round_sends_only_the_new_message_on_the_conversation():
    message = last_user_message(SECOND_TURN['input'])
    body = continuity_payload(SECOND_TURN, message, 'conv_abc')

    assert body == {
        'model': 'OWUI_Test_Skills',
        'stream': True,
        'instructions': 'You are helpful.',
        'input': [_message('user', 'now a docx')],
        'conversation_id': 'conv_abc',
    }
    assert 'tools' not in body and 'tool_choice' not in body
    # the original payload is left alone
    assert len(SECOND_TURN['input']) == 6 and 'tools' in SECOND_TURN


def test_continue_and_tool_follow_ups_end_without_a_user_message():
    assert last_user_message([_message('user', 'hi'), _message('assistant', 'partial')]) is None
    assert last_user_message([_message('user', 'hi'), {'type': 'function_call_output', 'call_id': 'c'}]) is None
    assert last_user_message([]) is None
    assert last_user_message(SECOND_TURN['input']) == _message('user', 'now a docx')


def test_task_and_fallback_bodies_are_stateless_plain_messages():
    task_payload = {
        'model': 'OWUI_Test_Skills',
        'stream': False,
        'input': [_message('user', 'Generate a title'), {'type': 'reasoning', 'id': 'rs_2'}],
    }
    body = stateless_payload(task_payload)
    assert body == {'model': 'OWUI_Test_Skills', 'stream': False, 'input': [_message('user', 'Generate a title')]}
    assert 'conversation_id' not in body

    fallback = stateless_payload({**SECOND_TURN, 'conversation_id': 'conv_old', 'previous_response_id': 'resp_1'})
    assert fallback['input'] == strip_history(SECOND_TURN['input'])
    assert not any(key in fallback for key in ('tools', 'tool_choice', 'conversation_id', 'previous_response_id'))


def test_conversation_ids_are_kept_per_model_in_the_chat_meta():
    meta = {'tags': ['work']}
    assert conversation_for('OWUI_Test_Skills', meta) is None
    meta = remember_conversation(meta, 'OWUI_Test_Skills', 'conv_1')
    meta = remember_conversation(meta, 'OWUI_Test_Basic', 'conv_2')
    assert meta == {
        'tags': ['work'],
        'quanterra': {'conversations': {'OWUI_Test_Skills': 'conv_1', 'OWUI_Test_Basic': 'conv_2'}},
    }
    assert conversation_for('OWUI_Test_Skills', meta) == 'conv_1'
    assert conversation_for('other', meta) is None
    assert conversation_for('x', None) is None


def test_stale_conversation_is_retried_once_on_a_fresh_one_else_the_stateless_body():
    fallback = stateless_payload(SECOND_TURN)

    assert retry_body(200, fallback, 'conv_new') is None  # nothing to retry
    assert retry_body(409, None, 'conv_new') is None  # task call or other provider
    assert retry_body(400, fallback, 'conv_new') is None  # not a stale conversation

    for status in (409, 410):
        body = retry_body(status, fallback, 'conv_new')
        assert body['conversation_id'] == 'conv_new'
        assert body['input'] == [_message('user', 'now a docx')]

    assert retry_body(409, fallback, None) is fallback  # creation failed: history as plain messages


def test_hook_leaves_other_providers_alone(monkeypatch):
    FakeStore().install(monkeypatch)
    payload = {'model': 'gpt-4o', 'input': SECOND_TURN['input']}
    body, fallback = asyncio.run(prepare(payload, {'api_type': 'responses'}, {'chat_id': 'chat-1'}, HEADERS, URL))
    assert body is payload and fallback is None


def test_hook_creates_the_conversation_once_per_chat_and_model_then_reuses_it(monkeypatch):
    store = FakeStore(meta={'tags': ['work']})
    store.install(monkeypatch)
    metadata = {'chat_id': 'chat-1'}

    body, fallback = asyncio.run(prepare(SECOND_TURN, QUANTERRA, metadata, HEADERS, URL))
    assert body['conversation_id'] == 'conv_1' and body['input'] == [_message('user', 'now a docx')]
    assert fallback == stateless_payload(SECOND_TURN)
    assert store.create_calls == [(URL, HEADERS)]
    assert store.meta == {'tags': ['work'], 'quanterra': {'conversations': {'OWUI_Test_Skills': 'conv_1'}}}

    body, _fallback = asyncio.run(prepare(SECOND_TURN, QUANTERRA, metadata, HEADERS, URL))
    assert body['conversation_id'] == 'conv_1' and len(store.create_calls) == 1


def test_hook_keeps_task_calls_unsaved_chats_and_continue_stateless(monkeypatch):
    store = FakeStore(meta={})
    store.install(monkeypatch)
    stateless = stateless_payload(SECOND_TURN)

    task = asyncio.run(prepare(SECOND_TURN, QUANTERRA, {'chat_id': 'chat-1', 'task': 'title_generation'}, HEADERS, URL))
    assert task == (stateless, None)

    store.meta = None  # temporary chat / channel: nothing to store the id in
    assert asyncio.run(prepare(SECOND_TURN, QUANTERRA, {'chat_id': 'temporary:x'}, HEADERS, URL)) == (stateless, None)

    store.meta = {}
    continued = {**SECOND_TURN, 'input': [_message('user', 'hi'), _message('assistant', 'partial')]}
    body, fallback = asyncio.run(prepare(continued, QUANTERRA, {'chat_id': 'chat-1'}, HEADERS, URL))
    assert body == stateless_payload(continued) and fallback is None
    assert store.create_calls == []


def test_hook_falls_back_to_stateless_when_the_runtime_cannot_create_a_conversation(monkeypatch):
    store = FakeStore(meta={}, create=())
    store.install(monkeypatch)
    body, fallback = asyncio.run(prepare(SECOND_TURN, QUANTERRA, {'chat_id': 'chat-1'}, HEADERS, URL))
    assert body == stateless_payload(SECOND_TURN) and fallback is None
    assert store.meta == {}


def test_stale_conversation_gets_one_fresh_conversation_and_the_turn_re_sent(monkeypatch):
    store = FakeStore(meta={'quanterra': {'conversations': {'OWUI_Test_Skills': 'conv_old'}}}, create=('conv_new',))
    store.install(monkeypatch)
    released = []
    stale = SimpleNamespace(status=409, release=lambda: released.append(True))
    fallback = stateless_payload(SECOND_TURN)

    body = asyncio.run(retry(stale, fallback, {'chat_id': 'chat-1'}, HEADERS, URL))
    assert body['conversation_id'] == 'conv_new' and body['input'] == [_message('user', 'now a docx')]
    assert released == [True]
    assert store.meta['quanterra']['conversations'] == {'OWUI_Test_Skills': 'conv_new'}

    ok = SimpleNamespace(status=200, release=lambda: released.append(True))
    assert asyncio.run(retry(ok, fallback, {'chat_id': 'chat-1'}, HEADERS, URL)) is None
    assert asyncio.run(retry(stale, None, {'chat_id': 'chat-1'}, HEADERS, URL)) is None
    assert len(released) == 1
