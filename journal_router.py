"""FastAPI adapter for the private journal API.

Deliberately thin: it reads the (size-capped) request body, hands plain values
to ``JournalService`` in a worker thread, and returns the service's response.
The route handlers declare no body or query models, so FastAPI never runs its
own validation on journal input and can therefore never echo it back in an
error; all validation and error shaping happens in ``journal_service``.

Authentication is injected: ``member_dependency`` must raise ``HTTPException``
for a missing/invalid member session and return a dict with the member's
``email`` otherwise (webhook_server passes its existing session check).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


async def _read_limited_body(request: Request, max_bytes: int):
    """Return the request body, or None if it exceeds ``max_bytes``."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        return None
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _send(result):
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)


def build_router(service, member_dependency):
    router = APIRouter(prefix="/member/journal", tags=["journal"])

    @router.get("/profile")
    async def get_profile(member: dict = Depends(member_dependency)):
        return _send(await run_in_threadpool(service.profile_get, member["email"]))

    @router.patch("/profile")
    async def update_profile(request: Request, member: dict = Depends(member_dependency)):
        body = await _read_limited_body(request, service.max_body_bytes)
        if body is None:
            return _send(service.payload_too_large())
        return _send(await run_in_threadpool(service.profile_update, member["email"], body))

    @router.get("/entries")
    async def list_entries(request: Request, member: dict = Depends(member_dependency)):
        query = dict(request.query_params)
        return _send(await run_in_threadpool(service.entries_list, member["email"], query))

    @router.post("/entries")
    async def create_entry(request: Request, member: dict = Depends(member_dependency)):
        body = await _read_limited_body(request, service.max_body_bytes)
        if body is None:
            return _send(service.payload_too_large())
        return _send(await run_in_threadpool(service.entry_create, member["email"], body))

    @router.patch("/entries/{entry_id}")
    async def update_entry(
        entry_id: str, request: Request, member: dict = Depends(member_dependency)
    ):
        body = await _read_limited_body(request, service.max_body_bytes)
        if body is None:
            return _send(service.payload_too_large())
        return _send(await run_in_threadpool(service.entry_update, member["email"], entry_id, body))

    @router.delete("/entries/{entry_id}")
    async def delete_entry(entry_id: str, member: dict = Depends(member_dependency)):
        return _send(await run_in_threadpool(service.entry_delete, member["email"], entry_id))

    @router.delete("/data")
    async def delete_all(member: dict = Depends(member_dependency)):
        return _send(await run_in_threadpool(service.delete_all, member["email"]))

    @router.get("/export")
    async def export_journal(member: dict = Depends(member_dependency)):
        return _send(await run_in_threadpool(service.export, member["email"]))

    return router
