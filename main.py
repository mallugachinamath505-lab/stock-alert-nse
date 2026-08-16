import os
import asyncio
import logging
import random
import string
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import requests
import yfinance as yf
from pymongo import MongoClient

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------------------------------------------
# 1. CONFIGURATION & SECRETS
# ----------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "stock_alert_db"

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client[DB_NAME]
    alerts_collection = db["alerts"]
    users_collection = db["users"] # NEW: Table for tracking UTRs and Codes
    logger.info("Connected to MongoDB successfully.")
except Exception as e:
    logger.error(f"MongoDB connection failed: {e}")
    alerts_collection = None
    users_collection = None

# ----------------------------------------------------
# 2. PYDANTIC DATA MODELS
# ----------------------------------------------------
class AlertModel(BaseModel):
    name: str
    phone: Optional[str] = "Verified"
    chat_id: str
    ticker: str
    target_price: float
    condition: str 

class AccessRequest(BaseModel):
    name: str
    chat_id: str
    utr: str

class VerifyCodeRequest(BaseModel):
    chat_id: str
    code: str

class ApproveRequest(BaseModel):
    chat_id: str

# ----------------------------------------------------
# 3. HELPER FUNCTIONS (Stock & Telegram)
# ----------------------------------------------------
def format_nse_ticker(ticker: str) -> str:
    ticker = ticker.strip().upper()
    if not ticker.endswith(".NS") and not ticker.endswith(".BO"):
        return f"{ticker}.NS"
    return ticker

def fetch_live_price(ticker: str) -> Optional[float]:
    try:
        formatted = format_nse_ticker(ticker)
        stock = yf.Ticker(formatted)
        info = stock.fast_info
        price = getattr(info, "last_price", None)
        if price is None:
            data = stock.history(period="1d", interval="1m")
            if not data.empty:
                price = float(data["Close"].iloc[-1])
        return round(float(price), 2) if price else None
    except Exception as err:
        return None

def send_telegram_alert(chat_id: str, message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        return False

# ----------------------------------------------------
# 4. BACKGROUND PRICE CHECKER WORKER
# ----------------------------------------------------
async def background_stock_monitor():
    while True:
        try:
            if alerts_collection is not None:
                active_alerts = list(alerts_collection.find({"triggered": False}))
                for alert in active_alerts:
                    ticker = alert.get("ticker")
                    target = float(alert.get("target_price", 0))
                    condition = alert.get("condition")
                    chat_id = alert.get("chat_id")
                    user_name = alert.get("name", "Trader")

                    current_price = fetch_live_price(ticker)
                    if current_price is None:
                        await asyncio.sleep(2)
                        continue

                    is_triggered = False
                    if condition == "ABOVE" and current_price >= target:
                        is_triggered = True
                    elif condition == "BELOW" and current_price <= target:
                        is_triggered = True

                    if is_triggered:
                        msg = (
                            f"🚨 <b>NSE Stock Alert Triggered!</b>\n\n"
                            f"👤 <b>User:</b> {user_name}\n"
                            f"📈 <b>Stock:</b> <code>{ticker}</code>\n"
                            f"🎯 <b>Target:</b> ₹{target} ({condition})\n"
                            f"💰 <b>Current Price:</b> ₹{current_price}\n\n"
                            f"⚡ <i>Automated notification by NSE Alert Pro</i>"
                        )
                        send_telegram_alert(chat_id, msg)
                        alerts_collection.update_one(
                            {"_id": alert["_id"]},
                            {"$set": {"triggered": True, "triggered_at_price": current_price}}
                        )
                    await asyncio.sleep(2) 
        except Exception:
            pass
        await asyncio.sleep(60) 

@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor_task = asyncio.create_task(background_stock_monitor())
    yield
    monitor_task.cancel()

app = FastAPI(title="NSE Stock Alert API", lifespan=lifespan)

# ----------------------------------------------------
# 5. NEW ADMIN & ACTIVATION ROUTES
# ----------------------------------------------------

# User Submits UTR
@app.post("/api/request-access")
async def request_access(req: AccessRequest):
    if users_collection is not None:
        users_collection.update_one(
            {"chat_id": req.chat_id},
            {"$set": {"name": req.name, "utr": req.utr, "status": "pending", "code": None}},
            upsert=True
        )
    return {"status": "success"}

# User Enters Secret Code
@app.post("/api/verify-code")
async def verify_code(req: VerifyCodeRequest):
    if users_collection is not None:
        user = users_collection.find_one({"chat_id": req.chat_id, "code": req.code, "status": "approved"})
        if user:
            return {"status": "success", "name": user.get("name")}
    raise HTTPException(status_code=400, detail="Invalid code or pending approval.")

# Admin Dashboard API: Get Pending UTRs
@app.get("/api/admin/pending")
async def get_pending():
    if users_collection is None: return []
    return list(users_collection.find({"status": "pending"}, {"_id": 0}))

# Admin Dashboard API: Approve UTR & Generate Code
@app.post("/api/admin/approve")
async def approve_request(req: ApproveRequest):
    if users_collection is not None:
        # Generate random 6 character code
        unique_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        
        users_collection.update_one(
            {"chat_id": req.chat_id},
            {"$set": {"status": "approved", "code": unique_code}}
        )
        # Send it directly to their Telegram bot!
        msg = f"✅ <b>Payment Verified!</b>\n\nYour unique Access Code is:\n<code>{unique_code}</code>\n\nPaste this on the website to unlock your dashboard."
        send_telegram_alert(req.chat_id, msg)
        
        return {"status": "success", "code": unique_code}
    return {"status": "error"}

# ----------------------------------------------------
# 6. EXISTING ROUTES
# ----------------------------------------------------
@app.get("/")
async def serve_index():
    return FileResponse("index.html")

# HIDDEN ADMIN PAGE
@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head><script src="https://cdn.tailwindcss.com"></script></head>
    <body class="bg-slate-900 text-slate-100 p-8">
        <div class="max-w-2xl mx-auto">
            <h1 class="text-3xl font-bold text-blue-400 mb-6">Admin: Pending Payments</h1>
            <div id="list" class="space-y-4">Loading...</div>
        </div>
        <script>
            async function loadPending() {
                const res = await fetch('/api/admin/pending');
                const data = await res.json();
                const list = document.getElementById('list');
                if(data.length === 0) { list.innerHTML = '<p class="text-slate-400">No pending payments.</p>'; return; }
                list.innerHTML = data.map(u => `
                    <div class="bg-slate-800 p-4 rounded-xl border border-slate-700">
                        <p class="text-sm text-slate-400">Name: <span class="font-bold text-white">${u.name}</span></p>
                        <p class="text-sm text-slate-400">Chat ID: <span class="font-bold text-white">${u.chat_id}</span></p>
                        <p class="text-lg text-emerald-400 mt-2 font-mono">UTR: ${u.utr}</p>
                        <button onclick="approve('${u.chat_id}')" class="mt-4 bg-emerald-600 hover:bg-emerald-500 font-bold py-2 px-4 rounded transition shadow-lg">
                            ✅ Verify UTR & Send Code
                        </button>
                    </div>
                `).join('');
            }
            async function approve(chat_id) {
                event.target.innerText = "Generating Code...";
                event.target.disabled = true;
                const res = await fetch('/api/admin/approve', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ chat_id: chat_id })
                });
                const result = await res.json();
                alert("Success! The code " + result.code + " was generated and sent to their Telegram.");
                loadPending();
            }
            loadPending();
        </script>
    </body>
    </html>
    """

@app.post("/api/alerts")
async def create_alert(alert: AlertModel):
    ticker = format_nse_ticker(alert.ticker)
    live_price = fetch_live_price(ticker)
    if live_price is None:
        raise HTTPException(status_code=400, detail="Invalid ticker symbol.")
    alert_doc = {
        "name": alert.name, "phone": alert.phone, "chat_id": alert.chat_id,
        "ticker": ticker, "target_price": alert.target_price, "condition": alert.condition,
        "triggered": False, "created_at_price": live_price
    }
    if alerts_collection is not None:
        alerts_collection.insert_one(alert_doc)
    return {"status": "success", "current_price": live_price}

@app.get("/api/alerts/active")
async def get_active_alerts():
    if alerts_collection is None: return []
    alerts = list(alerts_collection.find({"triggered": False}, {"_id": 0}))
    return alerts