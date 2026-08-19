from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.clients.data_service import DataServiceClient
from app.exceptions import DataServiceNotFoundError
from app.orchestration import OrchestrationPhase, create_task, get_task, run_campaign_flow
from app.schemas.campaign import CampaignCreate, CampaignStartResponse, CampaignStatusOut

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

_LEAD_STATUSES = ("new", "queued", "contacted", "replied", "rejected")

# Промежуточные фазы orchestration.py — повторный /start во время них не запускает вторую задачу.
_IN_PROGRESS_PHASES = (OrchestrationPhase.parsing, OrchestrationPhase.messaging)


@router.post("", status_code=201)
def create_campaign(payload: CampaignCreate) -> dict:
    client = DataServiceClient()
    try:
        return client.create_campaign(payload.model_dump(exclude_none=True))
    finally:
        client.close()


@router.post("/{campaign_id}/start", status_code=202, response_model=CampaignStartResponse)
def start_campaign(campaign_id: UUID, background_tasks: BackgroundTasks) -> CampaignStartResponse:
    existing = get_task(campaign_id)
    if existing is not None and existing.phase in _IN_PROGRESS_PHASES:
        return CampaignStartResponse(campaign_id=campaign_id, phase=existing.phase)

    task = create_task(campaign_id)
    background_tasks.add_task(run_campaign_flow, campaign_id)
    return CampaignStartResponse(campaign_id=campaign_id, phase=task.phase)


@router.get("/{campaign_id}", response_model=CampaignStatusOut)
def get_campaign_status(campaign_id: UUID) -> CampaignStatusOut:
    client = DataServiceClient()
    try:
        try:
            campaign = client.get_campaign(campaign_id)
        except DataServiceNotFoundError:
            raise HTTPException(status_code=404, detail=f"campaign {campaign_id} not found")

        stats = {status: len(client.list_leads(campaign_id, status)) for status in _LEAD_STATUSES}
    finally:
        client.close()

    task = get_task(campaign_id)
    phase = task.phase if task is not None else OrchestrationPhase.idle
    error = task.error if task is not None else None

    return CampaignStatusOut(campaign=campaign, phase=phase, error=error, stats=stats)
