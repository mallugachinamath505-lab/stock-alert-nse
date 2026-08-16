import asyncio
import urllib.request
import urllib.parse
from contextlib import asynccontextmanager
from bson import ObjectId
import certifi
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pymongo import MongoClient
import yfinance as yf
import requests

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8899102290:AAHtrEfLVk0eMgerHKJ44lnarvApBwQhDJQ"
MONGO_URI = "mongodb+srv://mallugachinamath505_db_user:aoKBQpO5XRXily4W@stockalert.wmexi7s.mongodb.net/?appName=stockalert"

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["stock_alerts_db"]
alerts_col = db["alerts"]

# Set up a browser disguise to bypass Yahoo Finance rate limits
yf_session = requests.Session()
yf_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})

# ==========================================
# TELEGRAM DISPATCHER
# ==========================================
def send_telegram_alert(chat_id: str, ticker: str, current_price: float, target_price: float, condition: str):
    message = (
        f"🚨 *NSE Stock Alert Triggered!* 🚨\n\n"
        f"📈 *Stock:* `{ticker}`\n"
        f"💰 *Current Price:* ₹{current_price:.2f}\n"
        f"🎯 *Target Condition:* {condition} ₹{target_price:.2f}\n\n"
        f"⚡ _Track live on NSE India_"
    )
    safe_message = urllib.parse.quote(message)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage?chat_id={chat_id}&text={safe_message}&parse_mode=Markdown"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        urllib.request.urlopen(req)
        print(f"📱 Telegram notification sent successfully to Chat ID: {chat_id}")
    except Exception as e:
        print(f"❌ Failed to send Telegram to {chat_id}: {e}")

# ==========================================
# 24/7 BACKGROUND WORKER
# ==========================================
async def check_prices_loop():
    while True:
        try:
            active_alerts = list(alerts_col.find({"is_active": True}))
            if active_alerts:
                print(f"\n[Worker] Checking {len(active_alerts)} active NSE alert(s)...")

            for alert in active_alerts:
                ticker = alert["ticker"]
                target_price = alert["target_price"]
                condition = alert["condition"]
                chat_id = alert["chat_id"]
                alert_id = alert["_id"]

                try:
                    # Pass the session disguise here
                    stock = yf.Ticker(ticker, session=yf_session)
                    current_price = stock.history(period="1d")['Close'].iloc[-1]

                    triggered = False
                    if condition == "BELOW" and current_price <= target_price:
                        triggered = True
                    elif condition == "ABOVE" and current_price >= target_price:
                        triggered = True

                    if triggered:
                        print(f"🚨 ALERT TRIGGERED: {ticker} @ ₹{current_price:.2f} for Chat ID {chat_id}")
                        send_telegram_alert(chat_id, ticker, current_price, target_price, condition)
                        alerts_col.update_one({"_id": alert_id}, {"$set": {"is_active": False}})
                    
                    # Pause for 15 seconds before checking the next stock to prevent bans
                    await asyncio.sleep(15)

                except Exception as e:
                    print(f"[Error] Failed checking {ticker}: {e}")

        except Exception as db_err:
            print(f"[Database Error]: {db_err}")

        # Poll every 5 minutes (300 seconds)
        await asyncio.sleep(300)

# ==========================================
# FASTAPI APPLICATION & ROUTES
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(check_prices_loop())
    yield
    task.cancel()

app = FastAPI(title="NSE Stock Alert Pro", lifespan=lifespan)

class AlertRequest(BaseModel):
    name: str
    phone: str
    chat_id: str
    ticker: str
    target_price: float
    condition: str

@app.get("/")
def serve_frontend():
    return FileResponse("index.html")

@app.post("/api/alerts")
def create_alert(alert: AlertRequest):
    ticker = alert.ticker.strip().upper()
    if not ticker.endswith('.NS'):
        ticker = f"{ticker}.NS"

    try:
        stock = yf.Ticker(ticker, session=yf_session)
        current_price = stock.history(period="1d")['Close'].iloc[-1]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid NSE stock symbol or Yahoo Finance is blocking the request. Try again.")

    new_alert = {
        "name": alert.name.strip(),
        "phone": alert.phone.strip(),
        "chat_id": alert.chat_id.strip(),
        "ticker": ticker,
        "target_price": alert.target_price,
        "condition": alert.condition,
        "is_active": True
    }
    result = alerts_col.insert_one(new_alert)

    return {
        "message": "Alert created successfully in cloud!",
        "current_price": round(current_price, 2),
        "id": str(result.inserted_id)
    }

@app.get("/api/alerts/active")
def get_active_alerts():
    alerts = []
    for doc in alerts_col.find({"is_active": True}):
        alerts.append({
            "id": str(doc["_id"]),
            "name": doc.get("name", "User"),
            "chat_id": doc["chat_id"],
            "ticker": doc["ticker"],
            "target": doc["target_price"],
            "condition": doc["condition"]
        })
    return alerts