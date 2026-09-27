"""Conversation continuity: what a Quanterra runtime receives per turn, and the hook's decisions."""

import asyncio
from types import SimpleNamespace

import pytest
from open_webui.quanterra import responses
from open_webui.quanterra.responses import (
    continuity_payload,
    conversation_for,
    conversation_gone,
    is_quanterra_connection,
    is_quanterra_model,
    is_task_call,
    last_user_message,
    prepare,
    remember_conversation,
    retry,
    seeded_payload,
    stateless_payload,
    strip_history,
)

QUANTERRA = {'api_type': 'responses', 'tags': [{'name': 'quanterra'}, {'name': 'quanterra:rt-1'}]}
HEADERS = {'Authorization': 'Bearer t', 'x-quenterra-thread-id': 'chat-1'}
URL = 'http://runtime:8088/responses'
USER = 'user-1'
UNKNOWN = '{"error": "unknown continuation id for this caller"}'
BUSY = '{"error": "conversation already has an active writer"}'


class FakeStore:
    """Stands in for the chat meta (DB) and the runtime's conversation endpoint."""

    def __init__(self, meta=None, create=('conv_1', 'conv_2')):
        self.meta = meta
        self.created = list(create)
        self.create_calls = []
        self.meta_calls = []

    def install(self, monkeypatch):
        async def load_meta(chat_id, user_id):
            self.meta_calls.append(('load', chat_id, user_id))
            return None if self.meta is None else dict(self.meta)

        async def save_meta(chat_id, user_id, meta):
            self.meta_calls.append(('save', chat_id, user_id))
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


def _response(status: int, body: str = '', released=None):
    sink = [] if released is None else released

    async def text():
        return body

    return SimpleNamespace(status=status, text=text, release=lambda: sink.append(True))


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
HISTORY = [_message('user', 'build an xlsx'), _message('assistant', 'Done: /mnt/data/owui-test.xlsx')]
NEW_MESSAGE = _message('user', 'now a docx')


def test_only_connections_tagged_quanterra_count():
    assert is_quanterra_connection({'tags': [{'name': 'quanterra'}, {'name': 'quanterra:rt-1'}]})
    assert is_quanterra_connection({'tags': ['quanterra']})
    assert not is_quanterra_connection({'tags': [{'name': 'openai'}]})
    assert not is_quanterra_connection({'api_type': 'responses'})
    assert not is_quanterra_connection(None)


def test_workspace_presets_on_a_quanterra_base_count_as_runtime_models():
    base = {'id': 'OWUI_Test_Basic', 'tags': [{'name': 'quanterra'}]}
    # utils/models.py builds the preset dict without the base's tags
    preset = {'id': 'support-bot', 'preset': True, 'info': {'base_model_id': 'OWUI_Test_Basic'}}
    models = {'OWUI_Test_Basic': base, 'support-bot': preset, 'gpt-4o': {'id': 'gpt-4o'}}

    assert is_quanterra_model(base, models)
    assert is_quanterra_model(preset, models)
    assert not is_quanterra_model({'id': 'gpt-4o'}, models)
    assert not is_quanterra_model({'id': 'x', 'info': {'base_model_id': 'gpt-4o'}}, models)
    assert not is_quanterra_model(preset, None)


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
        'input': [NEW_MESSAGE],
        'conversation_id': 'conv_abc',
    }
    assert 'tools' not in body and 'tool_choice' not in body
    # the original payload is left alone
    assert len(SECOND_TURN['input']) == 6 and 'tools' in SECOND_TURN


def test_continue_and_tool_follow_ups_end_without_a_user_message():
    assert last_user_message([_message('user', 'hi'), _message('assistant', 'partial')]) is None
    assert last_user_message([_message('user', 'hi'), {'type': 'function_call_output', 'call_id': 'c'}]) is None
    assert last_user_message([]) is None
    assert last_user_message(SECOND_TURN['input']) == NEW_MESSAGE


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
    assert fallback['input'] == [*HISTORY, NEW_MESSAGE]
    assert not any(key in fallback for key in ('tools', 'tool_choice', 'conversation_id', 'previous_response_id'))


def test_a_fresh_conversation_is_seeded_with_the_whole_history():
    stateless = stateless_payload(SECOND_TURN)
    body = seeded_payload(stateless, 'conv_new')
    assert body['input'] == [*HISTORY, NEW_MESSAGE] and body['conversation_id'] == 'conv_new'
    assert 'conversation_id' not in stateless


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


def test_only_an_unknown_conversation_or_410_replaces_the_stored_one():
    assert conversation_gone(409, UNKNOWN)
    assert conversation_gone(410, '')
    assert not conversation_gone(409, BUSY)  # previous turn still streaming / aborting
    assert not conversation_gone(409, '')  # idempotency replay, thread conflict
    assert not conversation_gone(400, UNKNOWN)


def test_hook_leaves_other_providers_and_chat_completions_connections_alone(monkeypatch):
    store = FakeStore(meta={})
    store.install(monkeypatch)
    payload = {'model': 'gpt-4o', 'input': SECOND_TURN['input']}
    body, fallback = asyncio.run(prepare(payload, {'api_type': 'responses'}, {'chat_id': 'chat-1'}, USER, HEADERS, URL))
    assert body is payload and fallback is None

    # a hand-tagged Chat Completions connection keeps its messages and tools
    chat_payload = {'model': 'OWUI_Test_Basic', 'messages': [{'role': 'user', 'content': 'hi'}], 'tools': []}
    body, fallback = asyncio.run(
        prepare(chat_payload, {**QUANTERRA, 'api_type': 'chat'}, {'chat_id': 'chat-1'}, USER, HEADERS, URL)
    )
    assert body is chat_payload and fallback is None
    assert store.meta_calls == []


def test_hook_seeds_a_new_conversation_with_the_history_then_sends_only_the_new_message(monkeypatch):
    store = FakeStore(meta={'tags': ['work']})
    store.install(monkeypatch)
    metadata = {'chat_id': 'chat-1'}

    body, fallback = asyncio.run(prepare(SECOND_TURN, QUANTERRA, metadata, USER, HEADERS, URL))
    assert body['conversation_id'] == 'conv_1' and body['input'] == [*HISTORY, NEW_MESSAGE]
    assert 'tools' not in body and 'tool_choice' not in body
    assert fallback == stateless_payload(SECOND_TURN)
    assert store.create_calls == [(URL, HEADERS)]
    assert store.meta == {'tags': ['work'], 'quanterra': {'conversations': {'OWUI_Test_Skills': 'conv_1'}}}
    assert store.meta_calls == [('load', 'chat-1', USER), ('save', 'chat-1', USER)]

    body, fallback = asyncio.run(prepare(SECOND_TURN, QUANTERRA, metadata, USER, HEADERS, URL))
    assert body['conversation_id'] == 'conv_1' and body['input'] == [NEW_MESSAGE]
    assert fallback == stateless_payload(SECOND_TURN) and len(store.create_calls) == 1


def test_hook_keeps_task_calls_unsaved_or_foreign_chats_and_continue_stateless(monkeypatch):
    store = FakeStore(meta={})
    store.install(monkeypatch)
    stateless = stateless_payload(SECOND_TURN)

    task = asyncio.run(
        prepare(SECOND_TURN, QUANTERRA, {'chat_id': 'chat-1', 'task': 'title_generation'}, USER, HEADERS, URL)
    )
    assert task == (stateless, None)

    store.meta = None  # temporary chat, channel, or another user's chat: nothing to store the id in
    assert asyncio.run(prepare(SECOND_TURN, QUANTERRA, {'chat_id': 'chat-1'}, USER, HEADERS, URL)) == (stateless, None)
    assert store.meta_calls[-1] == ('load', 'chat-1', USER)

    store.meta = {}
    continued = {**SECOND_TURN, 'input': [_message('user', 'hi'), _message('assistant', 'partial')]}
    body, fallback = asyncio.run(prepare(continued, QUANTERRA, {'chat_id': 'chat-1'}, USER, HEADERS, URL))
    assert body == stateless_payload(continued) and fallback is None
    assert store.create_calls == []


def test_hook_asks_the_user_to_sign_in_again_when_the_oauth_session_is_gone(monkeypatch):
    store = FakeStore(meta={})
    store.install(monkeypatch)
    no_bearer = {'x-quenterra-thread-id': 'chat-1'}
    with pytest.raises(PermissionError) as caught:
        asyncio.run(prepare(SECOND_TURN, QUANTERRA, {'chat_id': 'chat-1'}, USER, no_bearer, URL))
    # main.py shows str(exception) as the chat message's error
    assert str(caught.value) == responses.SIGN_IN_AGAIN
    assert store.meta_calls == [] and store.create_calls == []

    # other providers without a bearer are not Quanterra's business
    payload = {'model': 'gpt-4o', 'input': SECOND_TURN['input']}
    assert asyncio.run(prepare(payload, {'api_type': 'responses'}, {'chat_id': 'chat-1'}, USER, no_bearer, URL)) == (
        payload,
        None,
    )


def test_hook_falls_back_to_stateless_when_the_runtime_cannot_create_a_conversation(monkeypatch):
    store = FakeStore(meta={}, create=())
    store.install(monkeypatch)
    body, fallback = asyncio.run(prepare(SECOND_TURN, QUANTERRA, {'chat_id': 'chat-1'}, USER, HEADERS, URL))
    assert body == stateless_payload(SECOND_TURN) and fallback is None
    assert store.meta == {}


def test_transient_conflict_re_sends_the_history_stateless_and_keeps_the_conversation(monkeypatch):
    store = FakeStore(meta={'quanterra': {'conversations': {'OWUI_Test_Skills': 'conv_old'}}}, create=('conv_new',))
    store.install(monkeypatch)
    released = []
    fallback = stateless_payload(SECOND_TURN)

    body = asyncio.run(retry(_response(409, BUSY, released), fallback, {'chat_id': 'chat-1'}, USER, HEADERS, URL))
    assert body is fallback and released == [True]
    assert store.create_calls == [] and store.meta_calls == []
    assert store.meta['quanterra']['conversations'] == {'OWUI_Test_Skills': 'conv_old'}


def test_unknown_conversation_gets_one_fresh_conversation_seeded_with_the_history(monkeypatch):
    store = FakeStore(meta={'quanterra': {'conversations': {'OWUI_Test_Skills': 'conv_old'}}}, create=('conv_new',))
    store.install(monkeypatch)
    released = []
    fallback = stateless_payload(SECOND_TURN)

    body = asyncio.run(retry(_response(409, UNKNOWN, released), fallback, {'chat_id': 'chat-1'}, USER, HEADERS, URL))
    assert body == {**fallback, 'conversation_id': 'conv_new'}
    assert body['input'] == [*HISTORY, NEW_MESSAGE] and released == [True]
    assert store.meta['quanterra']['conversations'] == {'OWUI_Test_Skills': 'conv_new'}
    assert store.meta_calls == [('load', 'chat-1', USER), ('save', 'chat-1', USER)]

    # 410 replaces too; when creation fails the history goes out stateless
    body = asyncio.run(retry(_response(410, '', released), fallback, {'chat_id': 'chat-1'}, USER, HEADERS, URL))
    assert body is fallback and len(store.create_calls) == 2

    assert asyncio.run(retry(_response(200, '', released), fallback, {'chat_id': 'chat-1'}, USER, HEADERS, URL)) is None
    assert (
        asyncio.run(retry(_response(409, UNKNOWN, released), None, {'chat_id': 'chat-1'}, USER, HEADERS, URL)) is None
    )
    assert len(released) == 2
