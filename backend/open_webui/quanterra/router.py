"""Quanterra API routes, mounted at ``/api/v1/quanterra`` from ``main.py``."""

from fastapi import APIRouter, Depends

from open_webui.quanterra.version import FORK, UPSTREAM_TAG
from open_webui.utils.auth import get_verified_user

router = APIRouter()


@router.get('/info')
async def get_quanterra_info(user=Depends(get_verified_user)):
    """Identify the fork and its upstream base to a signed-in user."""
    return {'fork': FORK, 'upstream': UPSTREAM_TAG}
