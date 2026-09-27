"""Quanterra API routes, mounted at ``/api/v1/quanterra`` from ``main.py``."""

from fastapi import APIRouter, Depends, Request

from open_webui.quanterra.discovery import CONTROL_PLANE_URL, sync_runtime_connections
from open_webui.quanterra.version import FORK, UPSTREAM_TAG
from open_webui.utils.auth import get_verified_user

router = APIRouter()


@router.get('/info')
async def get_quanterra_info(user=Depends(get_verified_user)):
    """Identify the fork, its upstream base and whether runtime discovery is configured."""
    return {'fork': FORK, 'upstream': UPSTREAM_TAG, 'control_plane': bool(CONTROL_PLANE_URL)}


@router.post('/runtimes/sync')
async def sync_runtimes(request: Request, user=Depends(get_verified_user)):
    """Re-read the hosted runtimes from the control plane now, with the caller's token."""
    changed = await sync_runtime_connections(request, user, force=True)
    return {'changed': changed}
