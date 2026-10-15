from pydantic import BaseModel
from datetime import datetime

class Message(BaseModel):
    channel: str
    message_id: int
    text: str
    date: datetime

class JobPosting(BaseModel):
    message_id: int
    url: str | None = None
    title: str | None = None
    company: str | None = None
    stack: list[str] = []
    remote: bool | None = None
    degree_status: str = "unknown"
    salary_min: int | None = None
    salary_max: int | None = None
    currency: str | None = None
    match_score: float = 0.0
    ai_summary: str | None = None