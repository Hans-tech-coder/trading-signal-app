import requests
from bs4 import BeautifulSoup

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
    Scrapes Forex Factory's daily calendar for high-impact news for the specific currency pair.
    Returns a dictionary with status and warning messages.
    """
    base_cur = currency_pair[:3]
    quote_cur = currency_pair[3:6]
    target_currencies = [base_cur, quote_cur]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    
    url = "https://www.forexfactory.com/calendar.php?day=today"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return {"has_warning": False, "message": "Failed to scrape calendar from Forex Factory."}
            
        soup = BeautifulSoup(response.text, 'html.parser')
        events = soup.find_all('tr', class_='calendar__row')
        
        upcoming_high_impact = []
        last_time = ""
        
        for event in events:
            time_td = event.find('td', class_='calendar__time')
            if time_td:
                time_str = time_td.text.strip()
                if time_str:
                    last_time = time_str
                    
            impact_td = event.find('td', class_='calendar__impact')
            if impact_td:
                span = impact_td.find('span')
                if span:
                    impact_class = span.get('class', [])
                    if 'icon--ff-impact-red' in impact_class:
                        currency_td = event.find('td', class_='calendar__currency')
                        event_td = event.find('td', class_='calendar__event')
                        if currency_td and event_td:
                            currency = currency_td.text.strip()
                            event_name = event_td.text.strip()
                            
                            if currency in target_currencies:
                                upcoming_high_impact.append(f"{currency} at {last_time} ({event_name})")
                        
        if upcoming_high_impact:
            warning_msg = "HIGH IMPACT NEWS WARNING: " + " | ".join(upcoming_high_impact)
            return {"has_warning": True, "message": warning_msg}
        else:
            return {"has_warning": False, "message": "Clear: No high-impact news today for this pair."}
            
    except Exception as e:
        return {"has_warning": False, "message": f"Error fetching news: {e}"}

if __name__ == "__main__":
    print("Central Bank Rates:", get_central_bank_rates())
    print("Upcoming News (EURUSD):", check_upcoming_news("EURUSD"))
