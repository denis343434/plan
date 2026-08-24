from pydantic import BaseModel


class HeadlessConfig(BaseModel):
    headless: bool
