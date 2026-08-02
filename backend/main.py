from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import sys
import re
import yfinance as yf
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from google import genai
from google.genai import types
import subprocess
import json
import threading
import time
import asyncio
from contextlib import asynccontextmanager
import database
import sentiment
import news_engine
import mt5_engine
import risk_manager
from tick_listener import tick_listener_instance

# Setup Gemini Client
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# Add the TradingAgents repo to path so we can import it (kept for default_config if needed, but not required)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../TradingAgents')))

app = FastAPI(
    title="AlphaSignal Engine",
    description="Backend for generating trading signals using Gemini Vision + MT5 execution",
    version="2.0",
)

# Allow frontend to access the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def trailing_stop_loop():
    while True:
        try:
            mt5_engine.apply_smart_trailing_stop(atr_multiplier=1.5)
        except Exception as e:
            print(f"Trailing stop loop error: {e}")
        time.sleep(60)

async def trade_manager_loop():
    """Background task that runs every minute to manage active trades."""
    while True:
        try:
            active_trades = database.get_active_trades()
            if active_trades:
                # 1. Evaluate standard Win/Loss
                for t in active_trades:
                    status = mt5_engine.evaluate_ticket(t["ticket_id"])
                    if status in ["WON", "LOST"]:
                        database.update_trade_status(t["id"], status)
                
                # 2. Re-fetch active trades in case some were just closed above
                current_active = database.get_active_trades()
                
                # 3. Run Trade Manager (Expiry Auto-Close)
                closed = mt5_engine.run_trade_manager(current_active)
                if closed > 0:
                    pass # Handled in the engine
                
                # 4. Update ATR Cache for the Tick Listener instead of running the slow trailing stop here
                active_symbols = list(set([t["ticker"] for t in current_active]))
                mt5_engine.update_atr_cache(active_symbols)
                
                # 5. Ensure Tick Listener is tracking the right symbols
                if not tick_listener_instance.active and active_symbols:
                    tick_listener_instance.start(active_symbols)
                elif tick_listener_instance.active and not active_symbols:
                    tick_listener_instance.stop()
                
        except Exception as e:
            print(f"Trade Manager Loop Error: {e}")
            
        await asyncio.sleep(60) # Run every 60 seconds

@app.on_event("startup")
async def startup_event():
    # Start the async background task
    asyncio.create_task(trade_manager_loop())
    print("🚀 Background Trade Manager initialized and running.")
    print("⚡ Event-Driven Tick Listener Ready (Idle until trade opens)")

@app.on_event("shutdown")
def shutdown_event():
    tick_listener_instance.stop()

class ScanRequest(BaseModel):
    date: str
    account_balance: float = 1000.0
    risk_percentage: float = 1.0

class ExecuteTradeRequest(BaseModel):
    action: str
    symbol: str
    entry: str
    sl: str
    tp: str
    lot_size: float = None
    expiry: str = ""
    signal_id: int = None

# Using clean standard symbols for MetaTrader 5
MAJOR_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"]

def get_yf_symbol(symbol: str) -> str:
    """Helper to map MT5 symbols to Yahoo Finance for technical indicators."""
    if symbol == "XAUUSD" or symbol == "GOLD":
        return "GC=F"
    if not symbol.endswith("=X"):
        return symbol + "=X"
    return symbol

def get_top_pairs(date_str: str, limit: int = 5) -> list:
    """Finds the top most volatile/momentum-driven pairs over the last 10 days using MT5 live data."""
    pair_moves = []
    
    for pair in MAJOR_PAIRS:
        try:
            move = mt5_engine.get_10_day_volatility(pair)
            pair_moves.append((pair, move))
        except Exception as e:
            print(f"Error getting volatility for {pair}: {e}")
            continue
            
    # Sort pairs by absolute move, descending
    pair_moves.sort(key=lambda x: x[1], reverse=True)
    
    # Return just the pair names, up to the limit
    top_pairs = [p[0] for p in pair_moves[:limit]]
    
    # Fallback if empty
    if not top_pairs:
        return [MAJOR_PAIRS[0]]
        
    return top_pairs

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

def calculate_lot_size(entry_f: float, sl_f: float, risk_pct: float, pair: str) -> float:
    """Delegates to MT5 for accurate lot sizing using real broker specifications."""
    try:
        if "JPY" in pair and len(str(int(entry_f))) <= 3:
            # simple check if entry is roughly correct for JPY
            pass
        return mt5_engine.calculate_lot_size(pair, entry_f, sl_f, risk_pct / 100.0)
    except Exception as e:
        print(f"MT5 Lot size calculation error: {e}")
        return 0.01

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

def calculate_adx(ticker_symbol: str, period: int = 14) -> float:
    """Calculates ADX (Average Directional Index) to determine trend strength."""
    try:
        yf_sym = get_yf_symbol(ticker_symbol)
        ticker = yf.Ticker(yf_sym)
        hist = ticker.history(period="1mo")
        if len(hist) < period + 1: return 0.0
        
        adx_df = ta.adx(hist['High'], hist['Low'], hist['Close'], length=period)
        if adx_df is not None and not adx_df.empty:
            return round(float(adx_df[f'ADX_{period}'].iloc[-1]), 2)
        return 0.0
    except Exception as e:
        print(f"Error calculating ADX: {e}")
        return 0.0

def calculate_ema(ticker_symbol: str, period: int = 200) -> float:
    """Calculates EMA (Exponential Moving Average) to determine trend direction."""
    try:
        yf_sym = get_yf_symbol(ticker_symbol)
        ticker = yf.Ticker(yf_sym)
        # 200 EMA requires at least 200 bars, fetching 1 year
        hist = ticker.history(period="1y")
        if len(hist) < period: return 0.0
        
        ema_series = ta.ema(hist['Close'], length=period)
        if ema_series is not None and not ema_series.empty:
            return round(float(ema_series.iloc[-1]), 5)
        return 0.0
    except Exception as e:
        print(f"Error calculating EMA: {e}")
        return 0.0

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

    print("\n--- NEW SCAN REQUEST ---")
    print(f"[1/4] Auto-scanning market for the top 5 most volatile pairs...")
    top_pairs = get_top_pairs(req.date, limit=5)
    print(f"      Top pairs to scan: {top_pairs}")
    
    last_result = None
    for i, best_pair in enumerate(top_pairs):
        print(f"\n      --- Evaluating {best_pair} ---")
        result = await evaluate_single_pair(best_pair, req)
        if result["action"] != "HOLD":
            return result
        else:
            if i < len(top_pairs) - 1:
                print(f"      {best_pair} resulted in HOLD. Moving to next pair...")
            else:
                print(f"      {best_pair} resulted in HOLD. Finished scanning all pairs.")
            last_result = result
            
    # If all were HOLD
    if last_result:
        last_result["ticker"] = "MULTIPLE"
        last_result["reasoning"] = f"Scanned top pairs ({top_pairs}) but all resulted in HOLD. Last reasoning: {last_result.get('reasoning', '')}"
        return last_result
    
    raise HTTPException(status_code=500, detail="No pairs could be evaluated.")

async def evaluate_single_pair(best_pair: str, req: ScanRequest):
    
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
        print(f"[2/4] Connecting to TradingView Desktop for {tv_symbol} for Multi-Timeframe Analysis...")
        # Set Symbol
        run_tv_command(["symbol", tv_symbol])
        
        # Get Quote
        quote_data = run_tv_command(["quote"])
        current_price = quote_data.get("last", "Unknown")
        print(f"      Current Price: {current_price}")
        
        # Take Screenshots for Multi-Timeframe (Daily, 4-Hour, 1-Hour)
        print("      Capturing chart screenshots (1D, 4H, 1H)...")
        mtf_images = []
        for tf_code, tf_name in [("1D", "Daily"), ("240", "4-Hour"), ("60", "1-Hour")]:
            print(f"      Switching to {tf_name} timeframe...")
            run_tv_command(["timeframe", tf_code])
            
            # Architect Safeguard: Wait asynchronously for the chart to load
            await asyncio.sleep(1.5)
            
            screenshot_data = run_tv_command(["screenshot", "-r", "chart"])
            file_path = screenshot_data.get("file_path")
            
            if not file_path or not os.path.exists(file_path):
                raise Exception(f"Failed to capture TradingView screenshot for {tf_name}.")

            with open(file_path, "rb") as f:
                mtf_images.append(f.read())


        # Get Volatility (ATR)
        print("      Calculating Current Volatility (ATR)...")
        current_atr = calculate_atr(best_pair)
        print(f"      Current Daily ATR: {current_atr}")

        # Get Advanced Technical Indicators
        print("      Calculating Advanced Indicators...")
        vwap = calculate_vwap(best_pair)
        bb_upper, bb_mid, bb_lower = calculate_bollinger_bands(best_pair)
        adx_val = calculate_adx(best_pair)
        ema_val = calculate_ema(best_pair)
        
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
        
        print("      Analyzing News Sentiment with FinBERT...")
        finbert_data = news_engine.get_finbert_sentiment(tv_symbol)
        finbert_str = f"FinBERT News Sentiment: {finbert_data['sentiment']} (Score: {finbert_data['score']})"
        print(f"      {finbert_str}")
            
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
        print(f"      Technical Filters - ADX: {adx_val}, 200 EMA: {ema_val}")

        # --- PRE-SCAN FILTER LOGIC (Mathematical Bouncer) ---
        if 0 < adx_val < 25:
            reason = f"Market is Choppy/Ranging (ADX {adx_val} < 25). AI scan aborted to protect capital."
            print(f"      [FILTERED] {reason}")
            # Save to Database as HOLD
            database.save_signal(tv_symbol, "HOLD", "", "", "", 0.0, 0.0)
            return {
                "status": "success",
                "ticker": tv_symbol,
                "action": "HOLD",
                "entry": "", "tp": "", "sl": "", "lot_size": 0.0, "rrr": 0.0,
                "reasoning": reason,
                "currency_strength": currency_strength,
                "news_status": news_check,
                "macro": macro_str
            }
            
        trend_status = "Neutral"
        try:
            curr_f = float(current_price)
            if ema_val > 0:
                if curr_f < ema_val:
                    trend_status = f"Downtrend (Price {curr_f} is below 200 EMA {ema_val})"
                elif curr_f > ema_val:
                    trend_status = f"Uptrend (Price {curr_f} is above 200 EMA {ema_val})"
        except Exception:
            pass

        # Calculate Mathematical Support & Resistance for the AI dynamically based on TF
        print(f"      Calculating Macro Support/Resistance for {tf_label} timeframe...")
        support_level = 0.0
        resistance_level = 0.0
        try:
            yf_sym = get_yf_symbol(best_pair)
            ticker_obj = yf.Ticker(yf_sym)
            
            # Dynamic lookback based on the selected timeframe
            if tf_label == "1-Hour":
                # For 1H chart, look back 2 weeks using 1H candles
                hist_data = ticker_obj.history(period="14d", interval="1h")
                lookback_str = "14-Day (1H candles)"
            elif tf_label == "4-Hour":
                # For 4H chart, look back 2 months using daily candles
                hist_data = ticker_obj.history(period="60d", interval="1d")
                lookback_str = "60-Day (Daily candles)"
            else: # Daily or higher
                # For Daily chart, look back 1 year using weekly candles
                hist_data = ticker_obj.history(period="1y", interval="1wk")
                lookback_str = "1-Year (Weekly candles)"

            if not hist_data.empty:
                resistance_level = round(float(hist_data['High'].max()), 5)
                support_level = round(float(hist_data['Low'].min()), 5)
        except Exception as e:
            print(f"      Error calculating Support/Resistance: {e}")
            lookback_str = "Unknown"

        # --- PYTHON MATH GUARDRAIL ---
        # LLMs are bad at math. We pre-calculate if a 1:2 RRR is even possible
        # based on safe SL (VWAP) and TP bounds (Macro S/R).
        math_guardrail = ""
        try:
            cp_f = float(current_price)
            vwap_f = float(vwap)
            supp_f = float(support_level)
            res_f = float(resistance_level)
            
            # Kronos VWAP Overextension Filter
            vwap_dist = abs(cp_f - vwap_f) / vwap_f if vwap_f > 0 else 0
            
            if vwap_dist > 0.003:
                math_guardrail = f"\nMATHEMATICAL GUARDRAIL: Price is OVEREXTENDED from VWAP ({vwap_dist*100:.2f}% distance). This is highly susceptible to mean reversion. You MUST return HOLD."
            elif "Downtrend" in trend_status and cp_f < vwap_f:
                risk = vwap_f - cp_f
                req_reward = risk * 2
                req_tp = cp_f - req_reward
                dist_to_support = cp_f - supp_f
                if req_tp < supp_f:
                    math_guardrail = f"\nMATHEMATICAL GUARDRAIL: A SELL trade requires SL above VWAP ({vwap_f}), meaning Risk is {risk:.2f}. For a 1:2 RRR, TP must be at {req_tp:.2f}. However, Macro Support is at {supp_f:.2f} (only {dist_to_support:.2f} away). A 1:2 RRR is MATHEMATICALLY IMPOSSIBLE without breaking support. You MUST return HOLD."
                else:
                    math_guardrail = f"\nMATHEMATICAL GUARDRAIL: A SELL trade with SL above VWAP ({vwap_f}) requires TP at {req_tp:.2f}. This is ABOVE Macro Support ({supp_f:.2f}), so a 1:2 RRR IS mathematically possible."
            elif "Uptrend" in trend_status and cp_f > vwap_f:
                risk = cp_f - vwap_f
                req_reward = risk * 2
                req_tp = cp_f + req_reward
                dist_to_res = res_f - cp_f
                if req_tp > res_f:
                    math_guardrail = f"\nMATHEMATICAL GUARDRAIL: A BUY trade requires SL below VWAP ({vwap_f}), meaning Risk is {risk:.2f}. For a 1:2 RRR, TP must be at {req_tp:.2f}. However, Macro Resistance is at {res_f:.2f} (only {dist_to_res:.2f} away). A 1:2 RRR is MATHEMATICALLY IMPOSSIBLE without breaking resistance. You MUST return HOLD."
                else:
                    math_guardrail = f"\nMATHEMATICAL GUARDRAIL: A BUY trade with SL below VWAP ({vwap_f}) requires TP at {req_tp:.2f}. This is BELOW Macro Resistance ({res_f:.2f}), so a 1:2 RRR IS mathematically possible."
        except Exception as e:
            print(f"      Error calculating math guardrail: {e}")
            
        # 3. Vision Analysis with Gemini
        print(f"[3/4] Sending visual data to Gemini 3.1 Pro Vision...")
        prompt = f"""
You are an expert forex and commodities trader. 
I am providing you with 3 screenshots of the current charts for {tv_symbol} directly from TradingView, in chronological order: Daily, 4-Hour, and 1-Hour.
You MUST perform Top-Down Analysis: Use the Daily chart to understand the macro trend, the 4-Hour chart for medium-term momentum, and the 1-Hour chart to find optimal entries.
The current price is {current_price}.
The current Daily Average True Range (ATR) volatility is {current_atr}.
The recent Volume Weighted Average Price (VWAP) is {vwap}.
Bollinger Bands (20-day): Upper={bb_upper}, Middle={bb_mid}, Lower={bb_lower}.
Mathematical Macro Levels ({lookback_str}): Resistance={resistance_level}, Support={support_level}. Do NOT hallucinate support/resistance. Use these exact bounds for your analysis.
Trend Alignment (200 EMA): {trend_status}.
Currency Strength Overview (Multi-Timeframe): {cs_overview}.
{macro_str}.{math_guardrail}
{sentiment_str}.
{finbert_str}.

NEWS STATUS: {news_check['message']}

Analyze the visual chart, paying close attention to:
- Candlestick patterns
- Support and resistance levels
- Any visible indicators (moving averages, oscillators, custom scripts)
- Trend direction

Based on this visual evidence and the provided mathematical indicators, provide a trading signal.
Use the ATR, VWAP, and Bollinger Bands to dynamically set logical Take Profit (TP) and Stop Loss (SL) levels to avoid market noise and overexposure.

CRITICAL CONTRARIAN RULE: Use the Retail Sentiment data as a contrarian filter to avoid traps. If retail sentiment is heavily skewed (>65%) in one direction, you should strongly bias your trading signal toward the OPPOSITE direction (e.g., if >70% are Long, bias toward SHORT/SELL) or return "HOLD" if the chart does not support the contrarian view. Do not trade with the retail herd.

CRITICAL TREND RULE: You are mathematically bound by the 200 EMA Trend Alignment. Do NOT attempt to "catch falling knives" or "stand in front of a freight train".
- If the Trend Alignment says "Downtrend", you MUST ONLY look for SELL setups or HOLD. You are FORBIDDEN from issuing a BUY signal.
- If the Trend Alignment says "Uptrend", you MUST ONLY look for BUY setups or HOLD. You are FORBIDDEN from issuing a SELL signal.
- If it is "Neutral", you may choose.

CRITICAL VWAP RULE: VWAP is your primary magnet and fair-value anchor. Avoid entering trades if the price is significantly far from VWAP, as mean-reversion is highly likely. Follow the Mathematical Guardrail exactly if it says price is overextended.

CRITICAL FUNDAMENTAL RULE (FinBERT): 
- If FinBERT News Sentiment is "Positive" for the base currency (e.g., USD in XAUUSD means gold drops), it strongly biases against buying Gold. Ensure your trade direction aligns with fundamental sentiment or return "HOLD".

CRITICAL NEWS RULE (ABSOLUTE OVERRIDE): Look at the NEWS STATUS above. Each high-impact news event is tagged with either [PASSED] or [UPCOMING].
1. If ANY high-impact news event is tagged as [UPCOMING], this is a strict "No-Trade Zone". You MUST ABSOLUTELY return "HOLD" regardless of how perfect the chart or trend looks. Whipsaw risk overrides all technical setups.
2. If ALL high-impact news events for today are tagged as [PASSED], the whipsaw risk has subsided, and you may proceed with generating a BUY or SELL signal based on technicals.
CRITICAL RISK RULE: You MUST ensure that the Risk-to-Reward Ratio (RRR) of your selected TP and SL is at least 1:2. If a 1:2 ratio is not possible given the market structure, you MUST return "HOLD".

CRITICAL LIMIT ORDER RULE: Use Limit Orders when optimal. If the trend is strong but the current price is far from moving averages or support/resistance, DO NOT force a "BUY" or "SELL" (Market Order). Instead, output "BUY LIMIT" or "SELL LIMIT" at your desired pullback price. 

Output your response STRICTLY as a JSON object with the following schema:
{{
  "action": "BUY" or "SELL" or "BUY LIMIT" or "SELL LIMIT" or "HOLD",
  "entry": "suggested entry price or '' if HOLD. For limit orders, this must be your chosen pullback price",
  "tp": "suggested take profit or '' if HOLD",
  "sl": "suggested stop loss or '' if HOLD",
  "expiry_hours": integer (The total hours to keep the trade open before auto-expiring it. E.g., if your entry is based on the 1H chart, 24 hours is appropriate. If based on the 4H chart, 72 hours. If based on the Daily chart, 120 hours. Choose based on your entry timeframe),
  "reasoning": "A concise 2-3 sentence explanation of what you see on the chart and how you used the indicators (ATR, VWAP, etc.) that justifies this decision."
}}
"""
        # Call Gemini Vision
        contents = [
            types.Part.from_bytes(data=img, mime_type='image/png') for img in mtf_images
        ]
        contents.append(prompt)
        
        response = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            )
        )
        print("[4/4] Received analysis from AI.")
        print("--- RAW AI RESPONSE ---")
        print(response.text)
        print("-----------------------")
        
        # 4. Parse Response
        try:
            raw_text = response.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            elif raw_text.startswith("```"):
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()
            
            # Auto-fix JSON if Gemini returns trailing extra characters like "}\n}"
            while raw_text.endswith('}'):
                try:
                    ai_data = json.loads(raw_text)
                    break
                except json.JSONDecodeError:
                    raw_text = raw_text[:-1].strip()
            else:
                # If it doesn't end with '}', it might be abruptly cut off. Try adding '}'
                try:
                    ai_data = json.loads(raw_text)
                except json.JSONDecodeError:
                    try:
                        ai_data = json.loads(raw_text + '}')
                    except json.JSONDecodeError:
                        try:
                            ai_data = json.loads(raw_text + '"}')
                        except json.JSONDecodeError:
                            raise ValueError("Unrecoverable JSON format")
            
            action = ai_data.get("action", "HOLD").upper()
            entry = str(ai_data.get("entry", ""))
            tp = str(ai_data.get("tp", ""))
            sl = str(ai_data.get("sl", ""))
            expiry_val = ai_data.get("expiry_hours", "")
            reasoning = ai_data.get("reasoning", "Analyzed via TradingView chart vision.")
            
        except Exception as e:
            print(f"Error parsing JSON from Gemini: {response.text}")
            action = "HOLD"
            entry = ""
            tp = ""
            sl = ""
            expiry_val = ""
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
                    lot_size = calculate_lot_size(entry_f, sl_f, req.risk_percentage, tv_symbol)
            except Exception as e:
                print(f"Error calculating lot size / RRR: {e}")

        # Removed saving to Database here. We will only save upon execution.
        signal_id = 0

        # We pass the actual AI-generated numeric expiry hours directly back to the API.
        # This prevents ValueError in mt5_engine when reading from the database.
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
            "expiry": str(expiry_val), # Convert integer to string for API model format
            "currency_strength": currency_strength,
            "news_status": news_check,
            "macro": macro_str,
            "signal_id": signal_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/evaluate-trades")
def evaluate_trades():
    pending_trades = database.get_pending_trades()
    evaluated = 0
    
    for trade in pending_trades:
        trade_id = trade["id"]
        ticket_id = trade.get("ticket_id", 0)
        
        # If it has no ticket, it was never executed. We can't evaluate it truly.
        if not ticket_id or ticket_id == 0:
            continue
            
        # Native MT5 Check
        status = mt5_engine.evaluate_ticket(ticket_id)
        
        if status in ["WON", "LOST"]:
            database.update_trade_status(trade_id, status)
            evaluated += 1
            
    return {"status": "success", "evaluated_count": evaluated}

@app.get("/api/trade-stats")
def trade_stats():
    try:
        stats = database.get_trade_stats()
        return {"status": "success", "data": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/mt5-analytics")
def mt5_analytics():
    try:
        data = mt5_engine.get_account_analytics()
        return data
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

class ExecuteTradeRequest(BaseModel):
    action: str
    symbol: str
    entry: str
    sl: str
    tp: str
    lot_size: float = None
    signal_id: int = None
    expiry: str = ""

@app.post("/api/execute-trade")
def execute_trade_endpoint(req: ExecuteTradeRequest):
    try:
        # 1. Pass through Centralized Risk Manager First
        validation = risk_manager.validate_trade_request(req.action, req.symbol, req.entry, req.sl, req.tp)
        if not validation["valid"]:
            print(f"Risk Manager Blocked Trade: {validation['reason']}")
            raise HTTPException(status_code=400, detail=f"Risk Manager Blocked Trade: {validation['reason']}")
            
        print(f"Executing {req.action} for {req.symbol} via MT5 Engine with Lot: {req.lot_size}...")
        result = mt5_engine.execute_trade(req.action, req.symbol, req.entry, req.sl, req.tp, req.lot_size)
        if result["success"]:
            # Link the DB signal to the exact MT5 ticket
            ticket_id = result.get("ticket")
            if ticket_id:
                # Calculate RRR locally for the DB log
                rrr = 0.0
                try:
                    e_f, sl_f, tp_f = float(req.entry), float(req.sl), float(req.tp)
                    if abs(e_f - sl_f) > 0:
                        rrr = round(abs(e_f - tp_f) / abs(e_f - sl_f), 2)
                except Exception:
                    pass
                
                # Save signal only now that it's executed, using the actual executed volume from MT5
                print(f"Saving Trade to Database... Linking to MT5 Ticket ID {ticket_id}")
                actual_volume = result.get("volume", req.lot_size)
                signal_id = database.save_signal(req.symbol, req.action, req.entry, req.tp, req.sl, actual_volume, rrr, req.expiry)
                
                if signal_id:
                    database.link_signal_to_ticket(signal_id, ticket_id)
                    
            return {"status": "success", "details": result}
        else:
            raise HTTPException(status_code=500, detail=result.get("message", "Execution failed"))
    except Exception as e:
        print(f"Execute Trade Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
