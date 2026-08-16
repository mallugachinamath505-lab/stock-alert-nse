import os
import asyncio
import logging
import random
import string
import sqlite3
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import requests
import yfinance as yf

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------------------------------------------
# 1. CONFIGURATION & SECRETS
# ----------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
DB_FILE = "alerts.db"

# Initialize SQLite Database
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS alerts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT, phone TEXT, chat_id TEXT,
                  ticker TEXT, target_price REAL, condition TEXT,
                  triggered BOOLEAN, created_at_price REAL, triggered_at_price REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (chat_id TEXT PRIMARY KEY,
                  name TEXT, utr TEXT, status TEXT, code TEXT)''')
    conn.commit()
    conn.close()
    logger.info("SQLite Database Initialized.")

init_db()

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
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT id, ticker, target_price, condition, chat_id, name FROM alerts WHERE triggered=0")
            active_alerts = c.fetchall()
            
            for row in active_alerts:
                alert_id, ticker, target, condition, chat_id, user_name = row
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
                    c.execute("UPDATE alerts SET triggered=1, triggered_at_price=? WHERE id=?", (current_price, alert_id))
                    conn.commit()
                await asyncio.sleep(2) 
            conn.close()
        except Exception as e:
            logger.error(f"Background monitor error: {e}")
        await asyncio.sleep(60) 

@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor_task = asyncio.create_task(background_stock_monitor())
    yield
    monitor_task.cancel()

app = FastAPI(title="NSE Stock Alert API", lifespan=lifespan)

# ----------------------------------------------------
# 5. ADMIN & ACTIVATION ROUTES (SQLite)
# ----------------------------------------------------
@app.post("/api/request-access")
async def request_access(req: AccessRequest):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO users (chat_id, name, utr, status, code)
                     VALUES (?, ?, ?, 'pending', NULL)
                     ON CONFLICT(chat_id) DO UPDATE SET
                     name=excluded.name, utr=excluded.utr, status='pending', code=NULL''',
                  (req.chat_id, req.name, req.utr))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/verify-code")
async def verify_code(req: VerifyCodeRequest):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT name FROM users WHERE chat_id=? AND code=? AND status='approved'", (req.chat_id, req.code))
    row = c.fetchone()
    conn.close()
    if row:
        return {"status": "success", "name": row[0]}
    raise HTTPException(status_code=400, detail="Invalid code or pending approval.")

@app.get("/api/admin/pending")
async def get_pending():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chat_id, name, utr FROM users WHERE status='pending'")
    rows = c.fetchall()
    conn.close()
    return [{"chat_id": r[0], "name": r[1], "utr": r[2]} for r in rows]

@app.post("/api/admin/approve")
async def approve_request(req: ApproveRequest):
    unique_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET status='approved', code=? WHERE chat_id=?", (unique_code, req.chat_id))
    conn.commit()
    conn.close()
    
    msg = f"✅ <b>Payment Verified!</b>\n\nYour unique Access Code is:\n<code>{unique_code}</code>\n\nPaste this on the website to unlock your dashboard."
    send_telegram_alert(req.chat_id, msg)
    return {"status": "success", "code": unique_code}

# ----------------------------------------------------
# 6. EXISTING ROUTES
# ----------------------------------------------------
@app.get("/")
async def serve_index():
    return FileResponse("index.html")

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
                try {
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
                } catch (err) {
                    document.getElementById('list').innerHTML = '<p class="text-rose-400">Error loading data.</p>';
                }
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
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO alerts (name, phone, chat_id, ticker, target_price, condition, triggered, created_at_price)
                 VALUES (?, ?, ?, ?, ?, ?, 0, ?)''',
              (alert.name, alert.phone, alert.chat_id, ticker, alert.target_price, alert.condition, live_price))
    conn.commit()
    conn.close()
    return {"status": "success", "current_price": live_price}

@app.get("/api/alerts/active")
async def get_active_alerts():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT ticker, target_price, condition, chat_id, name FROM alerts WHERE triggered=0")
    rows = c.fetchall()
    conn.close()
    return [{"ticker": r[0], "target": r[1], "condition": r[2], "chat_id": r[3], "name": r[4]} for r in rows]