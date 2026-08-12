from typing import List, Optional

from pydantic import BaseModel, EmailStr


class SendMailRequest(BaseModel):
    to: List[EmailStr]
    subject: str
    body: str
    html: Optional[str] = None
    cc: Optional[List[EmailStr]] = None
    bcc: Optional[List[EmailStr]] = None


class SendMailResponse(BaseModel):
    success: bool
    message: str
