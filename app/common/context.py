from typing import Optional
from pydantic import BaseModel
from uuid import UUID


class AppContext(BaseModel):
    trace_id: UUID
    action: Optional[str] = ""
    actor: Optional[UUID] = ""

    def log_metadata(self):
        parts = []
        if self.trace_id:
            parts.append(f"trace_id: {self.trace_id}")
        if self.action:
            parts.append(f"action: {self.action}")
        if self.actor:
            parts.append(f"actor: {self.actor}")
        return " - ".join(parts)
