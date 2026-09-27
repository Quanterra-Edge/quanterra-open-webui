"""The pure parts of runtime discovery: URL shaping, naming and connection merging."""

from open_webui.quanterra.discovery import (
    OWNER_TAG,
    PUBLIC_READ,
    connection_base_url,
    connection_entry,
    container_reachable_url,
    is_owned,
    merge_connections,
    model_id_for,
    model_row,
)


def test_model_entry_is_public_and_marked_as_quanterra_owned():
    row = model_row(
        'support-agent',
        {'id': 'support', 'name': 'support', 'agent': 'support-agent', 'version': '3'},
        'http://host.docker.internal:8088',
        {'target_kind': 'agent', 'version': '3'},
    )

    assert row['id'] == 'support-agent' and row['base_model_id'] is None
    assert row['name'] == 'support-agent'
    assert (
        row['access_grants'] == PUBLIC_READ == [{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}]
    )
    assert row['meta'][OWNER_TAG] == {
        'runtime_id': 'support',
        'version': '3',
        'origin': 'http://host.docker.internal:8088',
        'target_kind': 'agent',
    }
    assert 'v3' in row['meta']['description'] and '(support)' in row['meta']['description']


def test_localhost_becomes_the_docker_host_only_inside_a_container():
    assert container_reachable_url('http://localhost:8088', in_container=True) == 'http://host.docker.internal:8088'
    assert container_reachable_url('http://127.0.0.1:8088/x', in_container=True) == 'http://host.docker.internal:8088/x'
    assert container_reachable_url('http://localhost:8088', in_container=False) == 'http://localhost:8088'
    assert container_reachable_url('https://runtime.example.test', in_container=True) == 'https://runtime.example.test'


def test_connection_url_stops_before_the_responses_segment():
    assert connection_base_url('http://host.docker.internal:8088', '/responses') == 'http://host.docker.internal:8088'
    assert (
        connection_base_url('http://host.docker.internal:8088/', '/v1/responses')
        == 'http://host.docker.internal:8088/v1'
    )
    assert connection_base_url('http://r', None) == 'http://r'


def test_model_ids_are_the_agent_name_with_the_stack_as_tie_breaker():
    assert model_id_for({'id': 'support', 'agent': 'support agent'}, set()) == 'support-agent'
    assert model_id_for({'id': 'support-2', 'agent': 'support-agent'}, {'support-agent'}) == 'support-agent.support-2'
    assert model_id_for({'id': 'bare', 'agent': '', 'name': ''}, set()) == 'bare'


def test_merge_replaces_only_quanterra_owned_connections():
    urls = ['https://api.openai.com/v1', 'http://old-runtime:8088']
    keys = ['sk-manual']
    configs = {'0': {'enable': True}, '1': connection_entry('old-agent', 'old')}
    desired = [('http://host.docker.internal:8088', connection_entry('support-agent', 'support'))]

    new_urls, new_keys, new_configs = merge_connections(urls, keys, configs, desired)

    assert new_urls == ['https://api.openai.com/v1', 'http://host.docker.internal:8088']
    assert new_keys == ['sk-manual', '']
    assert new_configs['0'] == {'enable': True}
    assert new_configs['1']['model_ids'] == ['support-agent']
    assert new_configs['1']['api_type'] == 'responses'
    assert new_configs['1']['auth_type'] == 'system_oauth'
    assert new_configs['1']['headers'] == {'x-quenterra-thread-id': '{{CHAT_ID}}'}
    assert is_owned(new_configs['1']) and not is_owned(new_configs['0'])
    assert {'name': OWNER_TAG} in new_configs['1']['tags']


def test_merge_with_nothing_discovered_removes_the_owned_connections_only():
    urls = ['http://gone:8088', 'https://api.openai.com/v1']
    configs = {'0': connection_entry('gone-agent', 'gone'), '1': {'enable': True}}

    assert merge_connections(urls, [], configs, []) == (['https://api.openai.com/v1'], [''], {'0': {'enable': True}})
