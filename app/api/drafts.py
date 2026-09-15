import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.auth_deps import get_current_user
from app.draft_service import (
    list_drafts,
    get_pending_drafts_count,
    get_draft_metrics,
    get_draft,
    update_draft,
    send_single_draft,
    discard_draft,
    batch_send_drafts,
    batch_send_by_filter,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Drafts"])


class UpdateDraftRequest(BaseModel):
    subject: Optional[str] = None
    draft_reply: Optional[str] = None
    to_email: Optional[str] = None


class DiscardDraftRequest(BaseModel):
    rejection_reason: Optional[str] = "Manually discarded"


class BatchSendDraftsRequest(BaseModel):
    draft_ids: List[int]


class BatchSendByFilterRequest(BaseModel):
    search: Optional[str] = None
    intent: Optional[str] = None
    sentiment: Optional[str] = None
    from_email: Optional[str] = None
    min_score: Optional[int] = None
    max_score: Optional[int] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None


@router.get("/drafts")
def get_drafts_endpoint(
    client_id: Optional[str] = None,
    status: Optional[str] = "pending",
    search: Optional[str] = None,
    intent: Optional[str] = None,
    sentiment: Optional[str] = None,
    from_email: Optional[str] = None,
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    user: dict = Depends(get_current_user)
):
    target_client = client_id or (None if user.get("role") == "admin" else user.get("client_id"))
    if user.get("role") != "admin" and target_client != user.get("client_id"):
        target_client = user.get("client_id")

    return list_drafts(
        client_id=target_client,
        status=status,
        search=search,
        intent=intent,
        sentiment=sentiment,
        from_email=from_email,
        min_score=min_score,
        max_score=max_score,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size
    )


@router.get("/drafts/count")
def get_pending_drafts_count_endpoint(client_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    target_client = client_id or (None if user.get("role") == "admin" else user.get("client_id"))
    if user.get("role") != "admin" and target_client != user.get("client_id"):
        target_client = user.get("client_id")
    count = get_pending_drafts_count(target_client)
    return {"pending_count": count}


@router.get("/drafts/metrics")
def get_draft_metrics_endpoint(client_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    target_client = client_id or (None if user.get("role") == "admin" else user.get("client_id"))
    if user.get("role") != "admin" and target_client != user.get("client_id"):
        target_client = user.get("client_id")
    return get_draft_metrics(target_client)


@router.get("/drafts/{draft_id}")
def get_single_draft_endpoint(draft_id: int, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    draft = get_draft(draft_id, client_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@router.put("/drafts/{draft_id}")
def update_draft_endpoint(draft_id: int, data: UpdateDraftRequest, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    ok = update_draft(
        draft_id=draft_id,
        client_id=client_id,
        subject=data.subject,
        draft_reply=data.draft_reply,
        to_email=data.to_email,
        reviewed_by=user.get("username", "Admin")
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to update draft or draft not pending")
    return {"status": "success", "message": "Draft updated successfully"}


@router.post("/drafts/{draft_id}/send")
def send_single_draft_endpoint(draft_id: int, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    res = send_single_draft(draft_id, client_id=client_id, reviewed_by=user.get("username", "Admin"))
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Failed to dispatch draft"))
    return res


@router.post("/drafts/{draft_id}/discard")
def discard_draft_endpoint(draft_id: int, data: DiscardDraftRequest, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    ok = discard_draft(draft_id, client_id=client_id, rejection_reason=data.rejection_reason, reviewed_by=user.get("username", "Admin"))
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to discard draft or draft not pending")
    return {"status": "success", "message": f"Draft #{draft_id} discarded"}


@router.post("/drafts/batch-send")
def batch_send_drafts_endpoint(data: BatchSendDraftsRequest, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    return batch_send_drafts(data.draft_ids, client_id=client_id, reviewed_by=user.get("username", "Admin"))


@router.post("/drafts/batch-send-filter")
def batch_send_by_filter_endpoint(data: BatchSendByFilterRequest, user: dict = Depends(get_current_user)):
    client_id = None if user.get("role") == "admin" else user.get("client_id")
    return batch_send_by_filter(data.dict(), client_id=client_id, reviewed_by=user.get("username", "Admin"))
