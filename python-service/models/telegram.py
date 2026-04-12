from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class TelegramMessage(BaseModel):
    """Payload for the /telegram/send endpoint."""

    text: str


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AnalyzeUrlRequest(BaseModel):
    """Request body for POST /jobs/analyze-url."""

    url: str
