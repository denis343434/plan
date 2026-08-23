import base64
import binascii
import mimetypes

from fastapi import APIRouter, HTTPException

from app.exceptions import DataServiceError, DataServiceNotFoundError, NoAccountAvailableError
from app.reply import send_reply
from app.schemas.reply import MAX_IMAGE_BYTES, ReplyRequest

router = APIRouter(prefix="/leads", tags=["reply"])


def _decode_image(request: ReplyRequest) -> dict | None:
    if request.image_base64 is None:
        return None

    try:
        raw = base64.b64decode(request.image_base64, validate=True)
    except binascii.Error as exc:
        raise HTTPException(status_code=400, detail=f"image_base64 is not valid base64: {exc}") from exc

    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"image is too large ({len(raw)} bytes, max {MAX_IMAGE_BYTES})",
        )

    mime_type, _ = mimetypes.guess_type(request.image_filename or "")
    if not mime_type or not mime_type.startswith("image/"):
        raise HTTPException(
            status_code=400, detail="only image attachments are supported (unrecognized image extension)"
        )

    return {"name": request.image_filename, "mimeType": mime_type, "buffer": raw}


@router.post("/{lead_id}/reply")
def reply_to_lead(lead_id: str, request: ReplyRequest) -> dict:
    image = _decode_image(request)
    try:
        return send_reply(lead_id, request.account_id, request.text, image=image)
    except DataServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NoAccountAvailableError as exc:
        raise HTTPException(status_code=409, detail=f"аккаунт сейчас занят другой задачей: {exc}") from exc
    except DataServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
