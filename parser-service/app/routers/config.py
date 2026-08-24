from fastapi import APIRouter

from app.config import settings
from app.schemas.config import HeadlessConfig

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/headless", response_model=HeadlessConfig)
def get_headless() -> HeadlessConfig:
    return HeadlessConfig(headless=settings.VK_HEADLESS)


@router.put("/headless", response_model=HeadlessConfig)
def set_headless(body: HeadlessConfig) -> HeadlessConfig:
    # Меняет режим только для playwright-браузеров, запускаемых ПОСЛЕ этого вызова (см.
    # adapters/vk.py: headless читается из settings в момент browser.launch()) — уже идущий
    # парсинг это не остановит и не переоткроет.
    settings.VK_HEADLESS = body.headless
    return HeadlessConfig(headless=settings.VK_HEADLESS)
