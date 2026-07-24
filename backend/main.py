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
import sentiment
import news_engine

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
    account_balance: float = 1000.0
    risk_percentage: float = 1.0

MAJOR_PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "XAUUSD=X"]

def get_yf_symbol(symbol: str) -> str:
    """Helper to map symbols for Yahoo Finance, particularly Gold which is often GC=F."""
    if symbol == "XAUUSD=X":
        return "GC=F"
    return symbol

def find_best_pair(date_str: str) -> str:
    """Finds the most volatile/momentum-driven pair over the last 5 days."""
    best_pair = MAJOR_PAIRS[0]
    max_move = -1
    
    end_date = datetime.strptime(date_str, "%Y-%m-%d")
    start_date = end_date - timedelta(days=10)
    
    for pair in MAJOR_PAIRS:
        try:
            yf_sym = get_yf_symbol(pair)
            ticker = yf.Ticker(yf_sym)
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
        yf_sym = get_yf_symbol(ticker_symbol)
        ticker = yf.Ticker(yf_sym)
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

def calculate_lot_size(account_balance: float, risk_pct: float, stop_loss_pips: float, pair: str) -> float:
    """Calculates MT5 lot size based on risk parameters."""
    if stop_loss_pips <= 0:
        return 0.01
    
    risk_amount = account_balance * (risk_pct / 100.0)
    
    # Standard lot size is 100,000 units. 
    # For pairs ending in USD (like EURUSD, XAUUSD), 1 pip = $10 per standard lot.
    # For JPY pairs, it varies, but we'll use a simplified baseline of $10 per pip per lot for now
    # to keep it straightforward, or adjust if we want exact precision.
    pip_value_per_lot = 10.0
    if "JPY" in pair:
        pip_value_per_lot = 1000 / 150.0 # Approximate JPY rate
    elif "XAUUSD" in pair:
        pip_value_per_lot = 100.0 # Gold pip value is often $1 per 0.01 lot -> $100 per 1.0 lot (standard points)
        
    lot_size = risk_amount / (stop_loss_pips * pip_value_per_lot)
    
    # Cap at standard MT5 limits (0.01 to 100)
    lot_size = max(0.01, min(round(lot_size, 2), 100.0))
    return lot_size

def calculate_vwap(ticker_symbol: str) -> float:
    """Calculates recent VWAP."""
    try:
        yf_sym = get_yf_symbol(ticker_symbol)
        ticker = yf.Ticker(yf_sym)
        hist = ticker.history(period="5d", interval="1h")
        if hist.empty: return 0.0
        
        hist['Typical_Price'] = (hist['High'] + hist['Low'] + hist['Close']) / 3
        hist['VP'] = hist['Typical_Price'] * hist['Volume']
        
        vwap = hist['VP'].sum() / hist['Volume'].sum() if hist['Volume'].sum() > 0 else hist['Close'].iloc[-1]
        return round(float(vwap), 5)
    except Exception:
        return 0.0

def calculate_bollinger_bands(ticker_symbol: str, period: int = 20) -> tuple:
    """Returns (Upper Band, Middle Band, Lower Band)"""
    try:
        yf_sym = get_yf_symbol(ticker_symbol)
        ticker = yf.Ticker(yf_sym)
        hist = ticker.history(period="1mo")
        if len(hist) < period: return (0.0, 0.0, 0.0)
        
        sma = hist['Close'].rolling(period).mean().iloc[-1]
        std = hist['Close'].rolling(period).std().iloc[-1]
        
        upper = sma + (std * 2)
        lower = sma - (std * 2)
        return round(float(upper), 5), round(float(sma), 5), round(float(lower), 5)
    except Exception:
        return (0.0, 0.0, 0.0)

def calculate_currency_strength() -> dict:
    """Returns currency strength across multiple timeframes (1H, 4H, 24H)."""
    currencies = {
        "EUR": "EURUSD=X",
        "GBP": "GBPUSD=X",
        "AUD": "AUDUSD=X",
        "JPY": "JPY=X", # USDJPY inverse
        "CAD": "CAD=X", # USDCAD inverse
        "CHF": "CHF=X", # USDCHF inverse
        "NZD": "NZDUSD=X"
    }
    
    timeframes = {
        "1H": {"period": "2d", "interval": "1h", "lookback": 1},
        "4H": {"period": "5d", "interval": "1h", "lookback": 4},
        "24H": {"period": "1mo", "interval": "1d", "lookback": 1}
    }
    
    results = {"1H": {}, "4H": {}, "24H": {}}
    
    try:
        # Pre-fetch data to avoid multiple calls per currency per timeframe
        hist_data = {}
        for cur, symbol in currencies.items():
            ticker = yf.Ticker(symbol)
            # Fetch 1h data
            hist_1h = ticker.history(period="5d", interval="1h")
            # Fetch 1d data
            hist_1d = ticker.history(period="1mo", interval="1d")
            hist_data[cur] = {"1h": hist_1h, "1d": hist_1d}

        for tf_name, tf_config in timeframes.items():
            performance = {}
            for cur, data in hist_data.items():
                hist = data["1h"] if tf_config["interval"] == "1h" else data["1d"]
                lookback = tf_config["lookback"]
                if len(hist) > lookback:
                    start_price = hist['Close'].iloc[-(lookback + 1)]
                    end_price = hist['Close'].iloc[-1]
                    change = (end_price - start_price) / start_price
                    if cur in ["JPY", "CAD", "CHF"]: 
                        change = -change # Inverse for USD base pairs
                    performance[cur] = round(float(change) * 100, 3) # As percentage
            
            # USD performance is inverse of average of others
            if performance:
                performance["USD"] = round(float(-sum(performance.values()) / len(performance)), 3)
                
            sorted_perf = sorted(performance.items(), key=lambda x: x[1], reverse=True)
            if sorted_perf:
                results[tf_name] = {
                    "strongest": sorted_perf[0][0],
                    "strongest_val": float(sorted_perf[0][1]),
                    "weakest": sorted_perf[-1][0],
                    "weakest_val": float(sorted_perf[-1][1]),
                    "all": {k: float(v) for k, v in sorted_perf}
                }
        return results
    except Exception as e:
        print(f"Error calculating currency strength: {e}")
        return {}

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
        # Calculate Currency Strength FIRST to determine dynamic timeframe
        print("      Calculating Currency Strength...")
        currency_strength = calculate_currency_strength()
        
        # Determine dynamic timeframe based on best_pair divergence
        tv_tf = "240" # Default 4H
        tf_label = "4-Hour"
        base_cur = tv_symbol[:3]
        quote_cur = tv_symbol[3:]
        
        if currency_strength and base_cur in currency_strength["1H"].get("all", {}) and quote_cur in currency_strength["1H"].get("all", {}):
            diff_1h = abs(currency_strength["1H"]["all"][base_cur] - currency_strength["1H"]["all"][quote_cur])
            diff_4h = abs(currency_strength["4H"]["all"][base_cur] - currency_strength["4H"]["all"][quote_cur])
            diff_24h = abs(currency_strength["24H"]["all"][base_cur] - currency_strength["24H"]["all"][quote_cur])
            
            max_diff = max(diff_1h, diff_4h, diff_24h)
            if max_diff == diff_1h:
                tv_tf = "60"
                tf_label = "1-Hour"
            elif max_diff == diff_4h:
                tv_tf = "240"
                tf_label = "4-Hour"
            else:
                tv_tf = "1D"
                tf_label = "Daily"
                
        print(f"      Selected dynamic timeframe: {tf_label} ({tv_tf}) based on momentum divergence.")

        # 2. Control TradingView via MCP CLI
        print(f"[2/4] Connecting to TradingView Desktop for {tv_symbol} on {tf_label}...")
        # Set Symbol
        run_tv_command(["symbol", tv_symbol])
        
        # Set Timeframe Dynamically
        run_tv_command(["timeframe", tv_tf])
        
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

        # Get Advanced Technical Indicators
        print("      Calculating Advanced Indicators...")
        vwap = calculate_vwap(best_pair)
        bb_upper, bb_mid, bb_lower = calculate_bollinger_bands(best_pair)
        
        print("      Fetching Retail Sentiment from Myfxbook...")
        sentiment_data = sentiment.get_myfxbook_sentiment(best_pair)
        
        if sentiment_data:
            sentiment_str = f"Retail Sentiment: {sentiment_data['long_percent']}% Long vs {sentiment_data['short_percent']}% Short (Dominant: {sentiment_data['dominant_bias']})"
        else:
            sentiment_str = "Retail Sentiment: Data Unavailable"
            
        print("      Checking Economic Calendar and Macro Context...")
        news_check = news_engine.check_upcoming_news(tv_symbol)
        cb_rates = news_engine.get_central_bank_rates()
        base_rate = cb_rates.get(base_cur, "N/A")
        quote_rate = cb_rates.get(quote_cur, "N/A")
        macro_str = f"Central Bank Rates: {base_cur} ({base_rate}%) vs {quote_cur} ({quote_rate}%)"
        
        print(f"      {macro_str}")
        print(f"      News Status: {news_check['message']}")
            
        print(f"      VWAP: {vwap}")
        print(f"      Bollinger Bands: Upper={bb_upper}, Mid={bb_mid}, Lower={bb_lower}")
        
        cs_str_list = []
        if currency_strength:
            for tf, data in currency_strength.items():
                if "strongest" in data:
                    cs_str_list.append(f"[{tf}] Strongest: {data['strongest']} (+{data['strongest_val']}%), Weakest: {data['weakest']} ({data['weakest_val']}%)")
        cs_overview = " | ".join(cs_str_list) if cs_str_list else "Data Unavailable"
        
        print(f"      Currency Strength: {cs_overview}")
        print(f"      {sentiment_str}")

        # 3. Vision Analysis with Gemini
        print(f"[3/4] Sending visual data to Gemini 3.1 Pro Vision...")
        current_time_str = datetime.now().strftime("%I:%M %p (Local Time)")
        
        prompt = f"""
You are an expert forex and commodities trader. 
I am providing you with a screenshot of the current {tf_label} chart for {tv_symbol} directly from TradingView.
We dynamically selected the {tf_label} timeframe because our Currency Strength Engine detected the highest divergence/momentum for {tv_symbol} on this timeframe.
The current price is {current_price}.
The current Daily Average True Range (ATR) volatility is {current_atr}.
The recent Volume Weighted Average Price (VWAP) is {vwap}.
Bollinger Bands (20-day): Upper={bb_upper}, Middle={bb_mid}, Lower={bb_lower}.
Currency Strength Overview (Multi-Timeframe): {cs_overview}.
{macro_str}.
{sentiment_str}.

NEWS STATUS: {news_check['message']}
CURRENT LOCAL TIME: {current_time_str}

Analyze the visual chart, paying close attention to:
- Candlestick patterns
- Support and resistance levels
- Any visible indicators (moving averages, oscillators, custom scripts)
- Trend direction

Based on this visual evidence and the provided mathematical indicators, provide a trading signal.
Use the ATR, VWAP, and Bollinger Bands to dynamically set logical Take Profit (TP) and Stop Loss (SL) levels to avoid market noise and overexposure.

CRITICAL CONTRARIAN RULE: Use the Retail Sentiment data as a contrarian filter to avoid traps. If retail sentiment is heavily skewed (>65%) in one direction, you should strongly bias your trading signal toward the OPPOSITE direction (e.g., if >70% are Long, bias toward SHORT/SELL) or return "HOLD" if the chart does not support the contrarian view. Do not trade with the retail herd.

CRITICAL TREND RULE: Do NOT attempt to "catch falling knives" or "stand in front of a freight train". If the price action is in a strong, established downtrend (consistently trading below the VWAP and Middle Bollinger Band), you MUST look for SELL setups or HOLD. Do not issue a BUY signal against a strong downtrend. Conversely, do not SELL into a strong uptrend.

CRITICAL NEWS RULE: If the NEWS STATUS above contains a "HIGH IMPACT NEWS WARNING", compare the news time to the CURRENT LOCAL TIME. If the news event has ALREADY PASSED today, the whipsaw risk has subsided, and you may proceed with generating a BUY or SELL signal based on technicals. However, if the high-impact news is still UPCOMING later today, you MUST return "HOLD" to avoid whipsaws.

CRITICAL RISK RULE: You MUST ensure that the Risk-to-Reward Ratio (RRR) of your selected TP and SL is at least 1:2. If a 1:2 ratio is not possible given the market structure, you MUST return "HOLD".

Output your response STRICTLY as a JSON object with the following schema:
{{
  "action": "BUY" or "SELL" or "HOLD",
  "entry": "suggested entry price or '' if HOLD",
  "tp": "suggested take profit or '' if HOLD",
  "sl": "suggested stop loss or '' if HOLD",
  "reasoning": "A concise 2-3 sentence explanation of what you see on the chart and how you used the indicators (ATR, VWAP, etc.) that justifies this decision."
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

        # Calculate Lot Size and RRR
        lot_size = 0.0
        rrr = 0.0
        if action != "HOLD" and entry and sl and tp:
            try:
                entry_f = float(entry)
                sl_f = float(sl)
                tp_f = float(tp)
                
                # Calculate stop loss distance in "standard points/pips" depending on asset
                sl_distance = abs(entry_f - sl_f)
                tp_distance = abs(entry_f - tp_f)
                
                if sl_distance > 0:
                    rrr = round(tp_distance / sl_distance, 2)
                    # For lot sizing, we need a standardized "pips" measure. 
                    if tv_symbol == "XAUUSD":
                        # In gold, 1.00 move is often considered 100 pips.
                        sl_pips = sl_distance
                    elif "JPY" in tv_symbol:
                        sl_pips = sl_distance * 100
                    else:
                        sl_pips = sl_distance * 10000
                        
                    lot_size = calculate_lot_size(req.account_balance, req.risk_percentage, sl_pips, tv_symbol)
            except Exception as e:
                print(f"Error calculating lot size / RRR: {e}")

        # Save to Database
        if action != "HOLD":
            database.save_signal(tv_symbol, action, entry, tp, sl, lot_size, rrr)

        return {
            "status": "success",
            "ticker": tv_symbol,
            "action": action,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "lot_size": lot_size,
            "rrr": rrr,
            "reasoning": reasoning,
            "currency_strength": currency_strength,
            "news_status": news_check,
            "macro": macro_str
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/evaluate-trades")
def evaluate_trades():
    pending_trades = database.get_pending_trades()
    evaluated = 0
    
    for trade in pending_trades:
        trade_id = trade["id"]
        date_gen = trade["date"]
        ticker = trade["ticker"]
        action = trade["action"]
        entry = trade["entry"]
        tp = trade["tp"]
        sl = trade["sl"]
        
        # Yahoo finance uses =X for forex
        y_ticker = ticker + "=X"
        yf_sym = get_yf_symbol(y_ticker)
        
        try:
            # Fetch data from date_generated to today with 1-hour interval for accurate intraday tracking
            # Give yfinance a 2-day future buffer to avoid timezone truncation issues with futures
            end_date = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
            data = yf.download(yf_sym, start=date_gen, end=end_date, interval="1h", progress=False)
            
            # Smart Fallback: If 1H intraday data is missing (common for Futures like Gold), use Daily data
            if data.empty:
                data = yf.download(yf_sym, start=date_gen, end=end_date, interval="1d", progress=False)
            
            if data.empty:
                continue
                
            highs = data['High'].values
            lows = data['Low'].values
            
            status = "PENDING"
            for i in range(len(highs)):
                # highs[i] could be a scalar or a 1D array depending on pandas version
                high_val = highs[i]
                low_val = lows[i]
                
                # Safely extract scalar value
                high = float(high_val.item() if hasattr(high_val, 'item') else high_val)
                low = float(low_val.item() if hasattr(low_val, 'item') else low_val)
                
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

@app.get("/api/analytics")
def get_analytics():
    try:
        data = database.get_advanced_analytics()
        return {"status": "success", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/ai-mentor")
def ask_ai_mentor():
    try:
        trades = database.get_all_trades_for_mentor()
        if not trades:
            return {"status": "success", "feedback": "You don't have enough completed trades yet. Keep trading so I can analyze your patterns!"}
        
        # Format trades for the prompt
        trade_history_str = json.dumps(trades, indent=2)
        
        prompt = f"""
You are an elite trading psychology coach and AI mentor.
I am providing you with the user's recent trade history (JSON format).

Analyze this data and provide a personalized 3-paragraph coaching summary:
1. Identify any recurring patterns in their winning vs losing trades (e.g., are they losing more on a specific pair? Are their stop losses too tight?).
2. Evaluate their Risk-to-Reward Ratio (RRR) discipline.
3. Provide an actionable piece of advice to improve their performance next week.

CRITICAL: Format your response as clean markdown. Keep it encouraging but strictly data-driven. Do not hallucinate data.

Trade History:
{trade_history_str}
"""
        
        response = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=[prompt]
        )
        
        return {"status": "success", "feedback": response.text}
    except Exception as e:
        print(f"AI Mentor Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate AI mentor feedback.")

