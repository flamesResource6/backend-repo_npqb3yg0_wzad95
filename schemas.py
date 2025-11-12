"""
Database Schemas for SmartPill

Each Pydantic model corresponds to a MongoDB collection. The collection name is the lowercase class name.
"""
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime

class User(BaseModel):
    name: str = Field(..., description="Full name")
    role: Literal["elder", "caregiver"] = Field(..., description="User role")
    linked_user_id: Optional[str] = Field(None, description="If caregiver, the elder user's id they monitor")

class MedicationSchedule(BaseModel):
    days_of_week: List[int] = Field(..., description="0=Mon ... 6=Sun")
    times: List[str] = Field(..., description="Times in 24h HH:MM format")

class Medication(BaseModel):
    user_id: str = Field(..., description="Owner elder user id")
    name: str
    dosage: str
    pill_image_url: Optional[str] = None
    schedule: MedicationSchedule

class DoseLog(BaseModel):
    user_id: str
    medication_id: str
    scheduled_at: datetime
    status: Literal["pending", "taken", "missed", "snoozed"] = "pending"
    taken_at: Optional[datetime] = None
    snooze_until: Optional[datetime] = None
