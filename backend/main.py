from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import sys
import re
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from google import genai
from google.genai import types
import subprocess
import json
import database

# Setup Gemini Client
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# Add the TradingAgents repo to path so we can import it (kept for default_config if needed, but not required)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../TradingAgents')))

app = FastAPI()

# Allow frontend to access the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScanRequest(BaseModel):
    date: str

MAJOR_PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "XAUUSD=X"]

def find_best_pair(date_str: str) -> str:
    """Finds the most volatile/momentum-driven pair over the last 5 days."""
    best_pair = MAJOR_PAIRS[0]
    max_move = -1
    
    end_date = datetime.strptime(date_str, "%Y-%m-%d")
    start_date = end_date - timedelta(days=10)
    
    for pair in MAJOR_PAIRS:
        try:
            ticker = yf.Ticker(pair)
            hist = ticker.history(start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"))
            if len(hist) >= 2:
                start_price = hist['Close'].iloc[0]
                end_price = hist['Close'].iloc[-1]
                move = abs((end_price - start_price) / start_price)
                if move > max_move:
                    max_move = move
                    best_pair = pair
        except Exception:
            continue
    return best_pair

def calculate_atr(ticker_symbol: str, period: int = 14) -> float:
    """Calculates the Average True Range (ATR) to measure volatility."""
    try:
        # Fetch enough data to calculate the ATR
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="1mo") # 1 month is enough for 14-day ATR
        
        if len(hist) < period + 1:
            return 0.0
            
        high_low = hist['High'] - hist['Low']
        high_close = (hist['High'] - hist['Close'].shift()).abs()
        low_close = (hist['Low'] - hist['Close'].shift()).abs()
        
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = ranges.max(axis=1)
        
        atr = true_range.rolling(period).mean().iloc[-1]
        return round(float(atr), 5)
    except Exception as e:
        print(f"Error calculating ATR for {ticker_symbol}: {e}")
        return 0.0

def run_tv_command(command_args):
    """Run a TradingView MCP CLI command and return JSON."""
    try:
        # Pass shell=True for windows environment resolution
        result = subprocess.run(
            ["tv"] + command_args, 
            capture_output=True, 
            text=True, 
            check=True,
            shell=True
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"TV CLI Error ({command_args}): {e.stderr}")
        raise Exception(f"TradingView automation failed: {e.stderr}")

@app.post("/api/scan-and-signal")
async def scan_and_signal(req: ScanRequest):
    if not os.getenv("GOOGLE_API_KEY"):
         raise HTTPException(status_code=500, detail="GOOGLE_API_KEY is not set in .env file.")

    # 1. Auto-Scan Market (Yahoo Finance)
    print("\n--- NEW SCAN REQUEST ---")
    print(f"[1/4] Auto-scanning market for the most volatile pair...")
    best_pair = find_best_pair(req.date)
    print(f"      Selected pair: {best_pair}")
    
    # Convert yahoo symbol to TradingView symbol (Basic mapping)
    tv_symbol = best_pair.replace("=X", "")

    try:
        # 2. Control TradingView via MCP CLI
        print(f"[2/4] Connecting to TradingView Desktop for {tv_symbol}...")
        # Set Symbol
        run_tv_command(["symbol", tv_symbol])
        
        # Set Timeframe to 4-Hour
        run_tv_command(["timeframe", "240"])
        
        # Get Quote
        quote_data = run_tv_command(["quote"])
        current_price = quote_data.get("last", "Unknown")
        print(f"      Current Price: {current_price}")
        
        # Take Screenshot
        print("      Capturing chart screenshot...")
        screenshot_data = run_tv_command(["screenshot", "-r", "chart"])
        file_path = screenshot_data.get("file_path")
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Failed to capture TradingView screenshot.")

        with open(file_path, "rb") as f:
            image_bytes = f.read()

        # Get Volatility (ATR)
        print("      Calculating Current Volatility (ATR)...")
        current_atr = calculate_atr(best_pair)
        print(f"      Current Daily ATR: {current_atr}")

        # 3. Vision Analysis with Gemini
        print(f"[3/4] Sending visual data to Gemini 3.1 Pro Vision...")
        prompt = f"""
You are an expert forex and commodities trader. 
I am providing you with a screenshot of the current 4-Hour chart for {tv_symbol} directly from TradingView.
The current price is {current_price}.
The current Daily Average True Range (ATR) volatility is {current_atr}.

Analyze the visual chart, paying close attention to:
- Candlestick patterns
- Support and resistance levels
- Any visible indicators (moving averages, oscillators, custom scripts)
- Trend direction

Based on this visual evidence and the provided ATR volatility metric, provide a trading signal.
Use the ATR to dynamically set logical Take Profit (TP) and Stop Loss (SL) levels to avoid market noise and overexposure.

Output your response STRICTLY as a JSON object with the following schema:
{{
  "action": "BUY" or "SELL" or "HOLD",
  "entry": "suggested entry price or '' if HOLD",
  "tp": "suggested take profit or '' if HOLD",
  "sl": "suggested stop loss or '' if HOLD",
  "reasoning": "A concise 2-3 sentence explanation of what you see on the chart and how you used ATR that justifies this decision."
}}
"""
        # Call Gemini Vision
        response = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=[
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type='image/png',
                ),
                prompt
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            )
        )
        print("[4/4] Received analysis from AI.")
        print("--- RAW AI RESPONSE ---")
        print(response.text)
        print("-----------------------")
        
        # 4. Parse Response
        try:
            ai_data = json.loads(response.text)
            action = ai_data.get("action", "HOLD").upper()
            
            if action == "HOLD":
                entry = ""
                tp = ""
                sl = ""
            else:
                entry = str(ai_data.get("entry", ""))
                tp = str(ai_data.get("tp", ""))
                sl = str(ai_data.get("sl", ""))
                
            reasoning = ai_data.get("reasoning", "Analyzed via TradingView chart vision.")
            
        except Exception as e:
            print(f"Error parsing JSON from Gemini: {response.text}")
            action = "HOLD"
            entry = ""
            tp = ""
            sl = ""
            reasoning = "Failed to parse AI visual analysis."

        # Save to Database
        if action != "HOLD":
            database.save_signal(tv_symbol, action, entry, tp, sl)

        return {
            "status": "success",
            "ticker": tv_symbol,
            "action": action,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "reasoning": reasoning
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/evaluate-trades")
def evaluate_trades():
    pending_trades = database.get_pending_trades()
    evaluated = 0
    
    for trade in pending_trades:
        trade_id = trade[0]
        date_gen = trade[1]
        ticker = trade[2]
        action = trade[3]
        entry = trade[4]
        tp = trade[5]
        sl = trade[6]
        
        # Yahoo finance uses =X for forex
        y_ticker = ticker + "=X"
        
        try:
            # Fetch data from date_generated to today with 1-hour interval for accurate intraday tracking
            # Give yfinance a 2-day future buffer to avoid timezone truncation issues with futures
            end_date = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
            data = yf.download(y_ticker, start=date_gen, end=end_date, interval="1h", progress=False)
            
            # Smart Fallback: If 1H intraday data is missing (common for Futures like Gold), use Daily data
            if data.empty:
                data = yf.download(y_ticker, start=date_gen, end=end_date, interval="1d", progress=False)
            
            if data.empty:
                continue
                
            highs = data['High'].values
            lows = data['Low'].values
            
            status = "PENDING"
            for i in range(len(highs)):
                high = float(highs[i])
                low = float(lows[i])
                
                if action == "BUY":
                    if high >= tp:
                        status = "WON"
                        break
                    elif low <= sl:
                        status = "LOST"
                        break
                elif action == "SELL":
                    if low <= tp:
                        status = "WON"
                        break
                    elif high >= sl:
                        status = "LOST"
                        break
                        
            if status != "PENDING":
                database.update_trade_status(trade_id, status)
                evaluated += 1
                
        except Exception as e:
            print(f"Error evaluating trade {trade_id}: {e}")
            
    return {"status": "success", "evaluated": evaluated}

@app.get("/api/trade-stats")
def trade_stats():
    try:
        stats = database.get_trade_stats()
        return {"status": "success", "data": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

