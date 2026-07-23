import cloudscraper
from bs4 import BeautifulSoup

def get_myfxbook_sentiment(symbol: str) -> dict:
    """
    Fetches the community sentiment (Long/Short ratio and volume) for a given symbol from Myfxbook.
    """
    url = 'https://www.myfxbook.com/community/outlook'
    
    try:
        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
        response = scraper.get(url, timeout=15)
        if response.status_code != 200:
            print(f"Failed to fetch Myfxbook outlook. Status code: {response.status_code}")
            return None
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Myfxbook uses =X in yahoo finance, but just the pair name in their UI
        clean_symbol = symbol.replace('=X', '').upper()
        
        row = soup.find('tr', {'class': 'outlook-symbol-row', 'symbolname': clean_symbol})
        if not row:
            print(f"Symbol {clean_symbol} not found in Myfxbook sentiment data.")
            return None
            
        # The detailed data is in a popover table within the row
        table = row.find('table', {'class': 'table'})
        if not table:
            print(f"Data table not found for {clean_symbol}.")
            return None
            
        rows = table.find('tbody').find_all('tr')
        if len(rows) < 2:
            return None
            
        # Parse Short (Row 0)
        short_data = rows[0].find_all('td')
        short_percent = int(short_data[2].text.strip().replace('%', ''))
        short_volume_lots = float(short_data[3].text.strip().split()[0])
        short_positions = int(short_data[4].text.strip())
        
        # Parse Long (Row 1)
        long_data = rows[1].find_all('td')
        long_percent = int(long_data[1].text.strip().replace('%', ''))
        long_volume_lots = float(long_data[2].text.strip().split()[0])
        long_positions = int(long_data[3].text.strip())
        
        return {
            "symbol": clean_symbol,
            "short_percent": short_percent,
            "long_percent": long_percent,
            "short_volume": short_volume_lots,
            "long_volume": long_volume_lots,
            "short_positions": short_positions,
            "long_positions": long_positions,
            "dominant_bias": "LONG" if long_percent > short_percent else "SHORT" if short_percent > long_percent else "NEUTRAL"
        }
        
    except Exception as e:
        print(f"Exception while scraping Myfxbook: {e}")
        return None

if __name__ == "__main__":
    # Test
    print("Testing EURUSD:")
    print(get_myfxbook_sentiment("EURUSD=X"))
