# app.py
import os
import json
import sqlite3
import base64
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from dotenv import load_dotenv
from flask import Flask, request, Response, jsonify
from flask_cors import CORS
from flask_sock import Sock
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client as TwilioClient

# websockets 15.x
import websockets.asyncio.client as ws_client

# Google Calendar
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

# ---------------- ENV ----------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2025-08-28")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://example.ngrok.app")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_CALLER_ID = os.getenv("TWILIO_CALLER_ID")  # Twilio number E.164

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "./google-credentials.json")
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]

CATALOG_PATH = os.getenv("CATALOG_PATH", "./catalog.json")
DB_PATH = os.getenv("LEADS_DB_PATH", "./leads.db")

# ---------------- Flask ----------------
app = Flask(__name__)
CORS(app)
sock = Sock(app)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ---------------- Load catalog ----------------
def load_catalog() -> Dict[str, Any]:
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

CATALOG = load_catalog()

# ---------------- SQLite lead store ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            city TEXT,
            address TEXT,
            email TEXT,
            service_key TEXT,
            requested_slot_iso TEXT,
            booked_slot_iso TEXT,
            calendar_event_id TEXT,
            notes TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def save_lead(lead: Dict[str, Any]) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO leads (name, phone, city, address, email, service_key, requested_slot_iso, booked_slot_iso, calendar_event_id, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lead.get("name"),
            lead.get("phone"),
            lead.get("city"),
            lead.get("address"),
            lead.get("email"),
            lead.get("service_key"),
            lead.get("requested_slot_iso"),
            lead.get("booked_slot_iso"),
            lead.get("calendar_event_id"),
            lead.get("notes"),
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid

# init DB on start
init_db()

# ---------------- Google Calendar helpers ----------------
def get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_JSON, scopes=CALENDAR_SCOPES
    )
    return build("calendar", "v3", credentials=creds)

def check_conflicts(calendar_id: str, start_iso: str, end_iso: str) -> bool:
    service = get_calendar_service()
    events = (
        service.events()
        .list(calendarId=calendar_id, timeMin=start_iso + "Z", timeMax=end_iso + "Z", singleEvents=True)
        .execute()
    )
    return len(events.get("items", [])) > 0

def create_calendar_event(calendar_id: str, summary: str, description: str, start_iso: str, end_iso: str, timezone: str = "America/Toronto") -> Dict[str, Any]:
    service = get_calendar_service()
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
    }
    created = service.events().insert(calendarId=calendar_id, body=event).execute()
    return created

def find_alternate_slot(calendar_id: str, desired_start_iso: str, duration_min: int) -> Dict[str, Any]:
    desired_start = datetime.fromisoformat(desired_start_iso)
    # search forward in 15m increments up to 8 hours
    step = timedelta(minutes=15)
    for i in range(1, 33):
        cand = desired_start + step * i
        cand_end = cand + timedelta(minutes=duration_min)
        if not check_conflicts(calendar_id, cand.isoformat(), cand_end.isoformat()):
            return {"start": cand.isoformat(), "end": cand_end.isoformat(), "is_alternate": True}
    # fallback: next day same time
    next_day = (desired_start + timedelta(days=1)).isoformat()
    return {"start": next_day, "end": (datetime.fromisoformat(next_day) + timedelta(minutes=duration_min)).isoformat(), "is_alternate": True}

# ---------------- System prompt / persona ----------------
SYSTEM_PROMPT = """
You are Vira, the professional AI receptionist for Kepsten.

ROLE (Persona & Mission)
- You are Vira, the professional AI receptionist for Kepsten.
- Outbound call goals: verify the lead (name, service, location, urgency, optional email).
- Calls must be short (2–3 minutes max).
- Always sound warm, polite, professional, and trustworthy.

OBJECTIVES
1. Engage quickly with a professional opening.
2. Verify the lead: name, service, location, urgency, email if possible.
3. Adapt naturally if name/service differs from the lead sheet.
4. Do not provide detailed pricing — only general starting ranges.
5. Escalate pricing/booking questions to a sales executive within 10 minutes.
6. Build credibility by highlighting Kepsten’s track record and guarantees.
7. Close politely and confirm next steps.

CALL FLOW
1. Opening: \"Hi, am I speaking with {{name}}?\"
2. Introduction: \"Hi {{name}}, this is Vira from Kepsten, Canada’s trusted home and business services platform. We received your inquiry about {{service}}, and I just need to confirm a few details. Is now a good time?\"
3. Verify: confirm service, location/postal code, urgency, and (if possible) email.
4. If voicemail: hang up immediately (no message).
5. If wrong service: ask \"What service are you looking for?\" and continue.
6. If not interested: thank them politely and highlight credibility (67,000+ projects done across Canada, 95%+ satisfaction).
7. If asked for pricing: say \"Some services start from $79, but exact pricing depends on details. One of our sales executives will call you within 10 minutes with accurate pricing.\"
8. Always end with: \"Thank you for choosing Kepsten. One of our sales team will be in touch shortly to confirm details.\"

GUARDRAILS
- Always remain respectful and professional, even if customer is rude.
- Do not oversell — focus on verifying details.
- Respect their time: if busy, offer callback.
- Handle objections gracefully; never argue or become defensive.
- If uncertain, be transparent: let them know a sales executive will follow up.
"""

# ---------------- Tools (exposed to model via session update) ----------------
TOOLS_SPEC = [
    {
        "type": "function",
        "name": "list_services",
        "description": "Return services from the local catalog with durations and prices. Optionally filter by keyword.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "type": "function",
        "name": "check_or_book_slot",
        "description": "Check availability for a requested date/time and service duration; optionally book the slot.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string"},
                "service_key": {"type": "string"},
                "book": {"type": "boolean"},
                "customer": {"type": "object", "properties": {"name": {"type": "string"}, "phone": {"type": "string"}, "city": {"type": "string"}, "address": {"type": "string"}, "email": {"type": "string"}}},
            },
            "required": ["start_iso", "service_key"],
        },
    },
]

def handle_tool_call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    # local tool implementations used by the model
    if tool_name == "list_services":
        q = (arguments or {}).get("query", "").lower()
        services = []
        for k, s in CATALOG.get("services", {}).items():
            if not q or q in k.lower() or q in s.get("name", "").lower():
                services.append({"key": k, "name": s["name"], "price_cad": s["price_cad"], "duration_min": s["duration_min"], "description": s.get("description", "")})
        return {"services": services}

    if tool_name == "check_or_book_slot":
        start_iso = arguments.get("start_iso")
        service_key = arguments.get("service_key")
        book = arguments.get("book", False)
        customer = arguments.get("customer", {})
        svc = CATALOG.get("services", {}).get(service_key)
        if not svc:
            return {"error": f"Unknown service {service_key}"}
        minutes = int(svc["duration_min"])
        # Check conflict
        desired_end = (datetime.fromisoformat(start_iso) + timedelta(minutes=minutes)).isoformat()
        conflict = check_conflicts(GOOGLE_CALENDAR_ID, start_iso, desired_end)
        if not conflict:
            if book:
                summary = f"{svc['name']} — {customer.get('name','Customer')}"
                description = f"Phone: {customer.get('phone','')}\nCity: {customer.get('city','')}\nAddress: {customer.get('address','')}\nEmail: {customer.get('email','')}\nService: {svc['name']}"
                created = create_calendar_event(GOOGLE_CALENDAR_ID, summary, description, start_iso, desired_end)
                return {"booked": True, "event_id": created.get("id"), "htmlLink": created.get("htmlLink"), "start": start_iso, "end": desired_end}
            else:
                return {"booked": False, "available": True, "start": start_iso, "end": desired_end}
        # conflict -> suggest alternate
        alt = find_alternate_slot(GOOGLE_CALENDAR_ID, start_iso, minutes)
        return {"booked": False, "available": False, "suggestion": alt}

    return {"error": f"Unknown tool {tool_name}"}

# ---------------- Twilio HTTP routes ----------------
@app.route("/call", methods=["POST"])
def place_call():
    to_number = request.args.get("to") or request.form.get("to")
    if not to_number:
        return jsonify({"error": "Missing 'to' parameter (E.164)."}), 400
    call = twilio_client.calls.create(to=to_number, from_=TWILIO_CALLER_ID, url=f"{PUBLIC_URL}/voice")
    return jsonify({"sid": call.sid})

@app.route("/voice", methods=["POST"])
def voice():
    """Return TwiML — Twilio will stream to /media (wss)."""
    response = VoiceResponse()
    # Twilio will stream to wss://<public>/media — Twilio uses the public host to reach your server
    response.say("Connecting you to Kepsten. Please hold.")
    connect = Connect()
    # Twilio expects a wss URL; request.host will be the host Twilio reached (ngrok domain). We prefer PUBLIC_URL.
    wss_url = PUBLIC_URL.replace("https://", "wss://")
    connect.stream(url=f"{wss_url}/media")
    response.append(connect)
    return Response(str(response), mimetype="application/xml")

# ---------------- Debug / Admin route ----------------
@app.route("/leads", methods=["GET"])
def get_leads():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, name, phone, city, address, email, service_key, requested_slot_iso, booked_slot_iso, calendar_event_id, notes, created_at FROM leads ORDER BY id DESC")
    rows = cur.fetchall()
    keys = ["id","name","phone","city","address","email","service_key","requested_slot_iso","booked_slot_iso","calendar_event_id","notes","created_at"]
    leads = [dict(zip(keys, r)) for r in rows]
    conn.close()
    return jsonify({"leads": leads})

# ---------------- Media WebSocket (Twilio <-> OpenAI Realtime) ----------------
# Note: This handler receives Twilio WebSocket frames (JSON). We forward audio to OpenAI realtime,
# and forward model audio back to Twilio. We also listen for function/tool calls and handle them with handle_tool_call
@sock.route("/media")
def media(ws):
    session_id = str(uuid.uuid4())
    print(f"[media] session {session_id} connected")

    async def openai_session():
        uri = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

        # Connect to OpenAI Realtime
        async with ws_client.connect(uri, additional_headers=headers) as oa:
            # create/update session with system prompt and tools
            session_update = {
                "type": "session.update",
                "session": {
                    "instructions": SYSTEM_PROMPT,
                    "input_audio_format": {"type": "g711_ulaw", "sampling_rate_hz": 8000},
                    "output_audio_format": {"type": "g711_ulaw", "sampling_rate_hz": 8000},
                    "turn_detection": {"type": "server_vad"},
                    "tools": TOOLS_SPEC,
                },
            }
            await oa.send(json.dumps(session_update))

            # helper to request a model response (audio + text)
            async def request_response():
                await oa.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio","text"]}}))

            # thread to read OpenAI -> forward to Twilio
            async def read_openai_loop():
                buffer_text = ""
                while True:
                    raw = await oa.recv()
                    if raw is None:
                        break
                    msg = json.loads(raw)
                    t = msg.get("type")
                    # tool call finished with arguments (model requested a tool)
                    if t == "response.function_call_arguments.done":
                        name = msg.get("name")
                        call_id = msg.get("call_id")
                        args_json = msg.get("arguments") or "{}"
                        try:
                            args = json.loads(args_json)
                        except Exception:
                            args = {}
                        result = handle_tool_call(name, args)
                        # send tool output back to OpenAI
                        tool_output = {"type": "tool.output", "call_id": call_id, "output": json.dumps(result)}
                        await oa.send(json.dumps(tool_output))
                        continue

                    # model text deltas
                    if t == "response.output_text.delta":
                        delta = msg.get("delta", "")
                        buffer_text += delta
                        # you can forward text to operator logs if desired

                    # when model produced audio chunks
                    if t == "response.output_audio.delta":
                        b64 = msg.get("audio", "")
                        if b64:
                            # Twilio expects base64 payloads inside media frames
                            ws.send(json.dumps({"event":"media", "media":{"payload": b64}}))

                    # response completed -> final text
                    if t == "response.completed":
                        final_text = msg.get("output", {}).get("text", buffer_text)
                        # Optionally save or log final_text
                        buffer_text = ""

            # twilio -> openai loop
            async def read_twilio_loop():
                # This handles Twilio events (start, media, stop)
                # We'll convert Twilio media payload frames to OpenAI input_audio_buffer.append with base64 audio
                while True:
                    try:
                        data = ws.receive()
                    except Exception:
                        data = None
                    if data is None:
                        break
                    try:
                        frame = json.loads(data)
                    except Exception:
                        continue
                    event = frame.get("event")
                    if event == "start":
                        # start of call
                        await request_response()
                    elif event == "media":
                        # Twilio gives base64 payload under media.payload — already base64 raw audio (g711u)
                        payload_b64 = frame.get("media", {}).get("payload")
                        if payload_b64:
                            # pass audio chunk to OpenAI
                            msg = {"type":"input_audio_buffer.append", "audio": payload_b64}
                            await oa.send(json.dumps(msg))
                            # commit chunk and ask model to respond (server_vad will help turn detection)
                            await oa.send(json.dumps({"type":"input_audio_buffer.commit"}))
                            await request_response()
                    elif event == "stop":
                        # end of call
                        break

            # run both loops concurrently
            await asyncio.gather(read_openai_loop(), read_twilio_loop())

    # run the async openai_session (sync context)
    try:
        asyncio.run(openai_session())
    except Exception as e:
        print("[media] openai_session error:", e)

    print(f"[media] session {session_id} disconnected")
    return ""

# ---------------- Run ----------------
if __name__ == "__main__":
    print("Starting app...")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
