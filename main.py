import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db, create_document, get_documents

app = FastAPI(title="SmartPill API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "SmartPill Backend Running"}


# Schemas for requests
class MedicationScheduleIn(BaseModel):
    days_of_week: List[int]
    times: List[str]

class MedicationIn(BaseModel):
    user_id: str
    name: str
    dosage: str
    pill_image_url: Optional[str] = None
    schedule: MedicationScheduleIn

class VoiceCommandIn(BaseModel):
    text: str
    user_id: str


# Helper functions

def collection(name: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    return db[name]


def compute_current_window(now: datetime):
    # determine next 30-min window start for matching scheduled times
    return now.replace(second=0, microsecond=0)


def get_todays_doses(user_id: str, now: Optional[datetime] = None):
    now = now or datetime.now(timezone.utc)
    weekday = (now.weekday())  # 0=Mon

    meds = list(collection("medication").find({"user_id": user_id}))

    due = []
    for m in meds:
        schedule = m.get("schedule", {})
        days = schedule.get("days_of_week", [])
        times = schedule.get("times", [])
        if weekday not in days:
            continue
        for t in times:
            try:
                hour, minute = map(int, t.split(":"))
            except Exception:
                continue
            sched_dt = now.astimezone(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
            # Create or find log
            log = collection("doselog").find_one({
                "user_id": user_id,
                "medication_id": str(m.get("_id")),
                "scheduled_at": sched_dt
            })
            status = log.get("status") if log else "pending"
            due.append({
                "medication_id": str(m.get("_id")),
                "name": m.get("name"),
                "dosage": m.get("dosage"),
                "pill_image_url": m.get("pill_image_url"),
                "scheduled_at": sched_dt.isoformat(),
                "status": status
            })
    # sort by time
    due.sort(key=lambda x: x["scheduled_at"])
    # Only return today's items
    today_str = now.date().isoformat()
    due = [d for d in due if d["scheduled_at"].startswith(today_str)]
    return due


@app.get("/api/today/{user_id}")
def today_meds(user_id: str):
    now = datetime.now(timezone.utc)
    doses = get_todays_doses(user_id, now)
    return {"items": doses}


@app.post("/api/medications")
def add_medication(payload: MedicationIn):
    med = payload.model_dump()
    med_id = create_document("medication", med)
    return {"id": med_id}


class TakeActionIn(BaseModel):
    user_id: str
    medication_id: str
    scheduled_at: datetime


@app.post("/api/take")
def mark_taken(payload: TakeActionIn):
    c = collection("doselog")
    q = {
        "user_id": payload.user_id,
        "medication_id": payload.medication_id,
        "scheduled_at": payload.scheduled_at
    }
    update = {"$set": {"status": "taken", "taken_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}}
    c.update_one(q, update, upsert=True)
    return {"ok": True}


class SnoozeIn(BaseModel):
    user_id: str
    medication_id: str
    scheduled_at: datetime
    minutes: int = 15


@app.post("/api/snooze")
def snooze(payload: SnoozeIn):
    until = datetime.now(timezone.utc) + timedelta(minutes=payload.minutes)
    c = collection("doselog")
    q = {
        "user_id": payload.user_id,
        "medication_id": payload.medication_id,
        "scheduled_at": payload.scheduled_at
    }
    update = {"$set": {"status": "snoozed", "snooze_until": until, "updated_at": datetime.now(timezone.utc)}}
    c.update_one(q, update, upsert=True)
    return {"ok": True, "snooze_until": until.isoformat()}


@app.post("/api/voice")
def voice_command(cmd: VoiceCommandIn):
    text_lower = cmd.text.lower().strip()

    if "what" in text_lower and ("medicine" in text_lower or "medication" in text_lower or "take now" in text_lower):
        now = datetime.now(timezone.utc)
        doses = get_todays_doses(cmd.user_id, now)
        # Choose nearest pending
        pending = [d for d in doses if d["status"] in ("pending", "snoozed")]
        if not pending:
            return {"response": "You have no medication due right now."}
        # find nearest by time difference
        def minutes_from_now(d):
            dt = datetime.fromisoformat(d["scheduled_at"])
            return abs(int((dt - now).total_seconds() // 60))
        pending.sort(key=minutes_from_now)
        top = pending[0]
        return {"response": f"It's medication time. Please take {top['dosage']} of {top['name']}."}

    if text_lower.startswith("remind") and "minute" in text_lower:
        # default 15 minutes
        minutes = 15
        for m in [5,10,15,20,30,45,60]:
            if f"{m}" in text_lower:
                minutes = m
                break
        now = datetime.now(timezone.utc)
        doses = get_todays_doses(cmd.user_id, now)
        pending = [d for d in doses if d["status"] in ("pending", "snoozed")]
        if not pending:
            return {"response": f"Okay, I'll remind you in {minutes} minutes, but there is nothing due right now."}
        top = pending[0]
        until = now + timedelta(minutes=minutes)
        # mark snoozed
        c = collection("doselog")
        q = {
            "user_id": cmd.user_id,
            "medication_id": top["medication_id"],
            "scheduled_at": datetime.fromisoformat(top["scheduled_at"]) 
        }
        c.update_one(q, {"$set": {"status": "snoozed", "snooze_until": until, "updated_at": now}}, upsert=True)
        return {"response": f"Okay, I'll remind you in {minutes} minutes."}

    return {"response": "Sorry, I didn't understand. You can ask: 'What medicine do I take now?' or say 'Remind for 15 minutes later.'"}


@app.get("/api/caregiver/compliance/{user_id}")
def caregiver_compliance(user_id: str):
    # Return last 30 days compliance grouped by day
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).date()
    logs = list(collection("doselog").find({"user_id": user_id}))
    day_status = {}
    for lg in logs:
        day = lg.get("scheduled_at")
        if isinstance(day, datetime):
            day = day.date()
        else:
            try:
                day = datetime.fromisoformat(day).date()
            except Exception:
                continue
        status = lg.get("status", "pending")
        # For a day, if any missed exists mark missed else if all taken mark taken else pending
        prev = day_status.get(day, {"taken":0,"missed":0,"pending":0})
        prev[status] = prev.get(status,0)+1
        day_status[day] = prev

    calendar = []
    d = start
    while d <= now.date():
        st = day_status.get(d, {"taken":0,"missed":0,"pending":0})
        symbol = "pending"
        if st.get("missed",0) > 0:
            symbol = "missed"
        elif st.get("taken",0) > 0 and st.get("pending",0) == 0:
            symbol = "taken"
        calendar.append({"date": d.isoformat(), "status": symbol})
        d += timedelta(days=1)

    return {"calendar": calendar}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        from database import db as dbcheck
        if dbcheck is not None:
            response["database"] = "✅ Connected & Working"
            response["database_url"] = "✅ Set"
            response["database_name"] = dbcheck.name
            response["connection_status"] = "Connected"
            try:
                response["collections"] = dbcheck.list_collection_names()[:10]
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:50]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    import os as _os
    response["database_url"] = "✅ Set" if _os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if _os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
