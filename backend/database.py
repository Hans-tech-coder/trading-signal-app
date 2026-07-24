import sqlite3
import os
from datetime import datetime

DB_FILE = os.path.join(os.path.dirname(__file__), "trades.db")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_generated TEXT,
            ticker TEXT,
            action TEXT,
            entry_price REAL,
            take_profit REAL,
            stop_loss REAL,
            lot_size REAL DEFAULT 0.0,
            rrr REAL DEFAULT 0.0,
            status TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_signal(ticker: str, action: str, entry: str, tp: str, sl: str, lot_size: float = 0.0, rrr: float = 0.0):
    if action == "HOLD" or not entry or not tp or not sl:
        return # Don't track HOLDs or invalid signals
        
    try:
        entry_price = float(entry)
        take_profit = float(tp)
        stop_loss = float(sl)
    except ValueError:
        return # Skip if not parseable

    date_generated = datetime.now().strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO signals (date_generated, ticker, action, entry_price, take_profit, stop_loss, lot_size, rrr, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (date_generated, ticker, action, entry_price, take_profit, stop_loss, lot_size, rrr, "PENDING"))
    conn.commit()
    conn.close()

def get_pending_trades():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT id, date_generated, ticker, action, entry_price, take_profit, stop_loss, status, lot_size, rrr FROM signals WHERE status = "PENDING" ORDER BY id DESC LIMIT 50')
    trades = cursor.fetchall()
    conn.close()
    
    result = []
    for t in trades:
        result.append({
            "id": t[0],
            "date": t[1],
            "ticker": t[2],
            "action": t[3],
            "entry": t[4],
            "tp": t[5],
            "sl": t[6],
            "status": t[7],
            "lot_size": t[8],
            "rrr": t[9]
        })
    return result

def update_trade_status(trade_id: int, status: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE signals SET status = ? WHERE id = ?', (status, trade_id))
    conn.commit()
    conn.close()

def get_trade_stats():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Get total won and lost
    cursor.execute('SELECT COUNT(*) FROM signals WHERE status = "WON"')
    won = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM signals WHERE status = "LOST"')
    lost = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM signals WHERE status = "PENDING"')
    pending = cursor.fetchone()[0]
    
    total_finished = won + lost
    win_rate = (won / total_finished * 100) if total_finished > 0 else 0
    
    # Get latest 10 trades
    cursor.execute('SELECT date_generated, ticker, action, status, lot_size, rrr FROM signals ORDER BY id DESC LIMIT 10')
    recent_trades = [{"date": r[0], "ticker": r[1], "action": r[2], "status": r[3], "lot_size": r[4], "rrr": r[5]} for r in cursor.fetchall()]
    
    conn.close()
    
    return {
        "won": won,
        "lost": lost,
        "pending": pending,
        "total": won + lost + pending,
        "win_rate": round(win_rate, 1),
        "recent_trades": recent_trades
    }

def get_advanced_analytics():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Win rate by Asset
    cursor.execute('''
        SELECT ticker, 
               COUNT(*) as total_trades,
               SUM(CASE WHEN status = "WON" THEN 1 ELSE 0 END) as won_trades
        FROM signals 
        WHERE status IN ("WON", "LOST")
        GROUP BY ticker
    ''')
    asset_stats = cursor.fetchall()
    
    asset_performance = {}
    for row in asset_stats:
        ticker = row[0]
        total = row[1]
        won = row[2]
        win_rate = (won / total * 100) if total > 0 else 0
        asset_performance[ticker] = {
            "total_trades": total,
            "win_rate": round(win_rate, 1)
        }
        
    conn.close()
    return {
        "asset_performance": asset_performance
    }

def get_all_trades_for_mentor():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Get the last 50 completed trades
    cursor.execute('SELECT date_generated, ticker, action, entry_price, take_profit, stop_loss, status, lot_size, rrr FROM signals WHERE status IN ("WON", "LOST") ORDER BY id DESC LIMIT 50')
    trades = cursor.fetchall()
    conn.close()
    
    result = []
    for t in trades:
        result.append({
            "date": t[0],
            "ticker": t[1],
            "action": t[2],
            "entry": t[3],
            "tp": t[4],
            "sl": t[5],
            "status": t[6],
            "lot_size": t[7],
            "rrr": t[8]
        })
    return result

# Initialize DB when this module is imported
init_db()
