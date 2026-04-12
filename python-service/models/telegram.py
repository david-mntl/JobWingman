from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class TelegramMessage(BaseModel):
    """Payload for the /telegram/send endpoint."""

    text: str
