import os
import asyncio
import logging
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import razorpay
import requests
import yfinance as yf
from pymongo import MongoClient

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------------------------------------------
# 1. CONFIGURATION & SECRETS
# ----------------------------------------------------
# Razorpay Test Credentials
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_TQTwme0xhHIyFO")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "TSawTzS4c6oWvGR0n3SOckYj")
rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# MongoDB Database Connection
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "stock_alert_db"

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client[DB_NAME]
    alerts_collection = db["alerts"]
    logger.info("Connected to MongoDB successfully.")
except Exception as e:
    logger.error(f"MongoDB connection failed: {e}")
    alerts_collection = None

# ----------------------------------------------------
# 2. PYDANTIC DATA MODELS
# ----------------------------------------------------
class AlertModel(BaseModel):
    name: str
    phone: Optional[str] = "Verified"
    chat_id: str
    ticker: str
    target_price: float
    condition: str  # "ABOVE" or "BELOW"

class PaymentVerification(BaseModel):
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str

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
        logger.error(f"Error fetching price for {ticker}: {err}")
        return None

def send_telegram_alert(chat_id: str, message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.warning("Telegram Bot Token is not configured.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
        return False

# ----------------------------------------------------
# 4. BACKGROUND PRICE CHECKER WORKER
# ----------------------------------------------------
async def background_stock_monitor():
    logger.info("Background stock price monitor started.")
    while True:
        try:
            if alerts_collection is not None:
                # Query active alerts
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

                    # Check trigger conditions
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
                        
                        # Mark alert as triggered so it does not repeat
                        alerts_collection.update_one(
                            {"_id": alert["_id"]},
                            {"$set": {"triggered": True, "triggered_at_price": current_price}}
                        )
                        logger.info(f"Alert triggered and sent for {ticker} to chat ID {chat_id}")

                    await asyncio.sleep(2)  # Delay between requests to avoid API rate limits

        except Exception as loop_err:
            logger.error(f"Error in background monitoring loop: {loop_err}")

        await asyncio.sleep(60)  # Wait 60 seconds before next scan loop

# ----------------------------------------------------
# 5. FASTAPI APPLICATION & LIFESPAN
# ----------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start background task
    monitor_task = asyncio.create_task(background_stock_monitor())
    yield
    # Shutdown
    monitor_task.cancel()

app = FastAPI(title="NSE Stock Alert Pro API", lifespan=lifespan)

# ----------------------------------------------------
# 6. ROUTE HANDLERS
# ----------------------------------------------------

# Serve Frontend HTML
@app.get("/")
async def serve_index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    raise HTTPException(status_code=404, detail="index.html not found")

# RAZORPAY: Create Order
@app.post("/api/create-order")
async def create_order():
    order_data = {
        "amount": 1000,  # 1000 paise = ₹10.00
        "currency": "INR",
        "receipt": "stock_alert_receipt",
        "payment_capture": 1
    }
    try:
        order = rzp_client.order.create(data=order_data)
        return {"order_id": order["id"]}
    except Exception as e:
        logger.error(f"Razorpay order creation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# RAZORPAY: Verify Payment Signature
@app.post("/api/verify-payment")
async def verify_payment(data: PaymentVerification):
    try:
        rzp_client.utility.verify_payment_signature({
            "razorpay_order_id": data.razorpay_order_id,
            "razorpay_payment_id": data.razorpay_payment_id,
            "razorpay_signature": data.razorpay_signature
        })
        return {"status": "success", "message": "Payment verified!"}
    except razorpay.errors.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ALERTS: Create a New Alert
@app.post("/api/alerts")
async def create_alert(alert: AlertModel):
    ticker = format_nse_ticker(alert.ticker)
    live_price = fetch_live_price(ticker)
    
    if live_price is None:
        raise HTTPException(status_code=400, detail=f"Invalid ticker symbol: '{alert.ticker}' or market data unavailable.")

    alert_doc = {
        "name": alert.name,
        "phone": alert.phone,
        "chat_id": alert.chat_id,
        "ticker": ticker,
        "target_price": alert.target_price,
        "condition": alert.condition,
        "triggered": False,
        "created_at_price": live_price
    }

    if alerts_collection is not None:
        alerts_collection.insert_one(alert_doc)
    
    return {
        "status": "success",
        "message": "Alert created successfully.",
        "current_price": live_price
    }

# ALERTS: List Active Alerts for Current User
@app.get("/api/alerts/active")
async def get_active_alerts():
    if alerts_collection is None:
        return []
    
    alerts = list(alerts_collection.find({"triggered": False}, {"_id": 0}))
    return [
        {
            "ticker": a.get("ticker"),
            "target": a.get("target_price"),
            "condition": a.get("condition"),
            "chat_id": a.get("chat_id"),
            "name": a.get("name")
        }
        for a in alerts
    ]