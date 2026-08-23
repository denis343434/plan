from pydantic import BaseModel, model_validator

# Ручной ответ — только картинки (см. обсуждение с пользователем), не документы/видео. Разумный
# запас над типичным размером фото из мессенджера/телефона, чтобы не гонять по сети base64
# на десятки мегабайт, которые VK всё равно не примет как разумное вложение к чату.
MAX_IMAGE_BYTES = 15 * 1024 * 1024


class ReplyRequest(BaseModel):
    account_id: str
    text: str = ""
    image_base64: str | None = None
    image_filename: str | None = None

    @model_validator(mode="after")
    def _text_or_image_required(self) -> "ReplyRequest":
        if not self.text.strip() and not self.image_base64:
            raise ValueError("text or image_base64 must be provided")
        if self.image_base64 and not self.image_filename:
            raise ValueError("image_filename is required when image_base64 is set")
        return self
