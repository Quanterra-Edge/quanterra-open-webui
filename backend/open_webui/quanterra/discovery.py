"""Hosted-runtime discovery: the control plane's runtimes become Responses connections.

The Quanterra control plane lists every hosted runtime it knows about
(``GET /api/deploy/runtimes``), wherever it runs. This module turns each one into
an Open WebUI "OpenAI" connection of type ``responses`` that forwards the signed-in
user's Keycloak token (``system_oauth``) and the chat id as the runtime's thread
header, so a hosted agent shows up in the model picker like any other model.

Connections this module writes carry the ``quanterra`` tag; that is how it tells
its own connections from ones an admin added by hand, which it never touches.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

CONTROL_PLANE_URL = os.environ.get('QUANTERRA_CONTROL_PLANE_URL', '').strip().rstrip('/')
RUNTIMES_PATH = '/api/deploy/runtimes?channel=responses'
RUNTIME_INFO_PATH = '/api/info'
THREAD_HEADER = 'x-quenterra-thread-id'
OWNER_TAG = 'quanterra'
SYNC_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10

_state: dict[str, Any] = {'last_sync': 0.0, 'lock': None}


# ------------------------------------------------------------------ pure parts


def container_reachable_url(url: str, in_container: bool | None = None) -> str:
    """Inside Docker, a localhost URL from the control plane means the Docker host."""
    if in_container is None:
        in_container = Path('/.dockerenv').exists()
    if not in_container:
        return url
    parts = urlsplit(url)
    if parts.hostname not in ('localhost', '127.0.0.1'):
        return url
    netloc = f'host.docker.internal:{parts.port}' if parts.port else 'host.docker.internal'
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def connection_base_url(origin: str, responses_route: str | None) -> str:
    """Open WebUI appends ``/responses`` itself, so the connection URL stops before it."""
    route = (responses_route or '/responses').strip().rstrip('/')
    if route.endswith('/responses'):
        route = route[: -len('/responses')]
    return origin.rstrip('/') + route


def model_id_for(runtime: dict[str, Any], taken: set[str]) -> str:
    """The agent's name, which stays stable across redeploys; the stack breaks a tie."""
    agent = str(runtime.get('agent') or runtime.get('name') or runtime.get('id') or '').strip()
    agent = agent.replace(' ', '-') or str(runtime.get('id'))
    if agent not in taken:
        return agent
    return f'{agent}.{runtime.get("id")}'


def connection_entry(model_id: str, runtime_id: str) -> dict[str, Any]:
    return {
        'enable': True,
        'api_type': 'responses',
        'auth_type': 'system_oauth',
        'connection_type': 'external',
        'model_ids': [model_id],
        'headers': {THREAD_HEADER: '{{CHAT_ID}}'},
        'tags': [{'name': OWNER_TAG}, {'name': f'{OWNER_TAG}:{runtime_id}'}],
    }


PUBLIC_READ = [{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}]


def model_row(model_id: str, runtime: dict[str, Any], origin: str, workflow: dict[str, Any]) -> dict[str, Any]:
    """The workspace model entry that makes the hosted agent visible to every signed-in user."""
    agent = str(runtime.get('agent') or model_id)
    version = str(runtime.get('version') or workflow.get('version') or '')
    stack = str(runtime.get('name') or runtime.get('id') or '')
    return {
        'id': model_id,
        'base_model_id': None,
        'name': agent,
        'meta': {
            'description': f'Quanterra hosted agent {agent}' + (f' v{version}' if version else '') + f' ({stack})',
            'tags': [{'name': 'Quanterra'}],
            OWNER_TAG: {
                'runtime_id': str(runtime.get('id') or ''),
                'version': version,
                'origin': origin,
                'target_kind': str(workflow.get('target_kind') or ''),
            },
        },
        'params': {},
        'access_grants': PUBLIC_READ,
        'is_active': True,
    }


def is_owned(config: dict[str, Any]) -> bool:
    tags = config.get('tags') or []
    return any((tag.get('name') if isinstance(tag, dict) else tag) == OWNER_TAG for tag in tags)


def merge_connections(
    urls: list[str],
    keys: list[str],
    configs: dict[str, Any],
    desired: list[tuple[str, dict[str, Any]]],
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Replace the Quanterra-owned connections with ``desired``; keep every other one as is."""
    keys = [*keys, *([''] * (len(urls) - len(keys)))][: len(urls)]
    kept = [
        (url, key, configs.get(str(index), {}))
        for index, (url, key) in enumerate(zip(urls, keys))
        if not is_owned(configs.get(str(index), {}))
    ]
    entries = [*kept, *((url, '', entry) for url, entry in desired)]
    return (
        [url for url, _key, _config in entries],
        [key for _url, key, _config in entries],
        {str(index): config for index, (_url, _key, config) in enumerate(entries) if config},
    )


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ----------------------------------------------------------------- async parts


async def _get_json(url: str, token: str | None = None) -> Any:
    import aiohttp

    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            return await response.json()


async def fetch_runtimes(token: str) -> list[dict[str, Any]]:
    payload = await _get_json(f'{CONTROL_PLANE_URL}{RUNTIMES_PATH}', token)
    return [row for row in payload.get('runtimes', []) if isinstance(row, dict)]


async def fetch_runtime_info(origin: str) -> dict[str, Any] | None:
    try:
        return await _get_json(f'{origin}{RUNTIME_INFO_PATH}')
    except Exception as exc:  # noqa: BLE001 - an unreachable runtime is simply not listed
        log.info('quanterra runtime %s skipped: %s', origin, exc)
        return None


async def desired_connections(token: str) -> list[dict[str, Any]]:
    """One connection and one public model entry per reachable hosted runtime, named after its agent."""
    runtimes = [row for row in await fetch_runtimes(token) if row.get('access_url')]
    origins = [container_reachable_url(str(row['access_url'])) for row in runtimes]
    infos = await asyncio.gather(*(fetch_runtime_info(origin) for origin in origins))
    desired: list[dict[str, Any]] = []
    taken: set[str] = set()
    for runtime, origin, info in zip(runtimes, origins, infos):
        if not info:
            continue
        workflow = info.get('workflow') or {}
        channel = (info.get('channels') or {}).get('responses') or {}
        if workflow.get('name'):
            runtime = {
                **runtime,
                'agent': workflow['name'],
                'version': workflow.get('version') or runtime.get('version'),
            }
        model_id = model_id_for(runtime, taken)
        taken.add(model_id)
        desired.append(
            {
                'url': connection_base_url(origin, channel.get('route')),
                'entry': connection_entry(model_id, str(runtime['id'])),
                'row': model_row(model_id, runtime, origin, workflow),
            }
        )
    return desired


async def sync_model_rows(rows: list[dict[str, Any]], user_id: str) -> bool:
    """Every hosted agent gets a public model entry; entries of vanished runtimes go."""
    from open_webui.models.models import ModelForm, Models

    existing = {model.id: model for model in await Models.get_all_models()}
    wanted = {row['id'] for row in rows}
    changed = False
    for row in rows:
        current = existing.get(row['id'])
        current_meta = _meta_dict(current.meta) if current else {}
        if current is None:
            await Models.insert_new_model(ModelForm(**row), user_id)
            changed = True
        elif current.name != row['name'] or current_meta.get(OWNER_TAG) != row['meta'][OWNER_TAG]:
            await Models.update_model_by_id(row['id'], ModelForm(**row))
            changed = True
    for model_id, model in existing.items():
        if model_id not in wanted and _meta_dict(model.meta).get(OWNER_TAG):
            await Models.delete_model_by_id(model_id)
            changed = True
    return changed


def _meta_dict(meta: Any) -> dict[str, Any]:
    if isinstance(meta, dict):
        return meta
    if meta is None:
        return {}
    return meta.model_dump() if hasattr(meta, 'model_dump') else dict(meta)


async def sync_runtime_connections(request: Any, user: Any, *, force: bool = False) -> bool:
    """Refresh the Quanterra connections at most once a minute; True when they changed."""
    if not CONTROL_PLANE_URL:
        return False
    if _state['lock'] is None:
        _state['lock'] = asyncio.Lock()
    async with _state['lock']:
        if not force and time.monotonic() - _state['last_sync'] < SYNC_INTERVAL_SECONDS:
            return False
        from open_webui.models.config import Config
        from open_webui.routers.openai import clear_openai_model_cache, get_openai_runtime_config
        from open_webui.utils.middleware import get_system_oauth_token

        token = await get_system_oauth_token(request, user)
        access_token = (token or {}).get('access_token') if isinstance(token, dict) else None
        if not access_token:
            log.info('quanterra runtime sync skipped: no system OAuth token for user %s', getattr(user, 'id', '?'))
            return False
        try:
            desired = await desired_connections(access_token)
        except Exception as exc:  # noqa: BLE001 - the control plane being down must not break the model list
            log.warning('quanterra runtime discovery failed: %s', exc)
            return False
        _state['last_sync'] = time.monotonic()

        rows_changed = await sync_model_rows([item['row'] for item in desired], str(getattr(user, 'id', '')))
        _enabled, urls, keys, configs = await get_openai_runtime_config()
        new_urls, new_keys, new_configs = merge_connections(
            list(urls), list(keys), dict(configs), [(item['url'], item['entry']) for item in desired]
        )
        if _same([urls, configs], [new_urls, new_configs]) and _enabled:
            if rows_changed:
                await clear_openai_model_cache(request)
            return rows_changed
        await Config.upsert(
            {
                'openai.enable': True,
                'openai.api_base_urls': new_urls,
                'openai.api_keys': new_keys,
                'openai.api_configs': new_configs,
            }
        )
        await clear_openai_model_cache(request)
        log.info('quanterra runtime connections synced: %d hosted agent(s)', len(desired))
        return True
