import requests
from datetime import datetime
import pytz
import json
import os
import pytz

def get_central_bank_rates():
    """Returns hardcoded/dynamic central bank rates for macro context."""
    return {
        "USD": 5.50,
        "EUR": 4.25,
        "GBP": 5.25,
        "CAD": 4.75,
        "AUD": 4.35,
        "NZD": 5.50,
        "CHF": 1.50,
        "JPY": 0.10
    }

def check_upcoming_news(currency_pair: str):
    """
    Fetches Forex Factory's weekly JSON calendar, filters for today's high-impact news 
    for the specific currency pair, and tags them as [PASSED] or [UPCOMING] based on PHT.
    """
    base_cur = currency_pair[:3]
    quote_cur = currency_pair[3:6]
    target_currencies = [base_cur, quote_cur]
    
    # In case of XAU or XAG, we typically care about USD news.
    if base_cur in ["XAU", "XAG"]:
        target_currencies = [quote_cur] # usually USD
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    }
    
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    cache_file = "news_cache.json"
    events = None
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            events = response.json()
            # Save to cache
            try:
                with open(cache_file, "w") as f:
                    json.dump(events, f)
            except Exception:
                pass
    except Exception as e:
        pass # Will attempt cache fallback below
        
    if not events:
        # Fallback to cache if request failed or rate limited (429)
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    events = json.load(f)
            except Exception:
                pass
                
    if not events:
        return {"has_warning": False, "message": "Failed to scrape JSON calendar from Forex Factory and no local cache available."}
        
    try:
        ph_tz = pytz.timezone('Asia/Manila')
        now_ph = datetime.now(ph_tz)
        today_date = now_ph.date()
        
        upcoming_high_impact = []
        
        for e in events:
            if e['impact'] == 'High' and e['country'] in target_currencies:
                # Parse date string: "2026-07-26T19:50:00-04:00"
                # datetime.fromisoformat handles the timezone offset perfectly
                try:
                    event_dt = datetime.fromisoformat(e['date'])
                    
                    # Convert to Philippine Time
                    event_dt_ph = event_dt.astimezone(ph_tz)
                    
                    # Check if the news is strictly for TODAY in Philippine Time
                    if event_dt_ph.date() == today_date:
                        time_str_ph = event_dt_ph.strftime('%I:%M%p').lower()
                        
                        if event_dt_ph < now_ph:
                            status = " [PASSED]"
                        else:
                            status = " [UPCOMING]"
                            
                        upcoming_high_impact.append(f"{e['country']} at {time_str_ph}{status} ({e['title']})")
                except ValueError:
                    pass # Skip if date format is unexpected

        if upcoming_high_impact:
            warning_msg = "HIGH IMPACT NEWS WARNING: " + " | ".join(upcoming_high_impact)
            return {"has_warning": True, "message": warning_msg}
        else:
            return {"has_warning": False, "message": "Clear: No high-impact news today for this pair."}
            
    except Exception as e:
        return {"has_warning": False, "message": f"Error fetching news: {e}"}

if __name__ == "__main__":
    print("Central Bank Rates:", get_central_bank_rates())
    print("Upcoming News (XAUUSD):", check_upcoming_news("XAUUSD"))
    print("Upcoming News (EURUSD):", check_upcoming_news("EURUSD"))
