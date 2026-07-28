import MetaTrader5 as mt5
import math
import threading
from datetime import datetime, timedelta

def initialize_mt5():
    if not mt5.initialize():
        print("initialize() failed, error code =", mt5.last_error())
        return False
    return True

def calculate_lot_size(symbol, entry_price, sl_price, risk_percentage=0.01):
    if not initialize_mt5():
        return 0.01
        
    account_info = mt5.account_info()
    if account_info is None:
        print("Failed to get account info")
        return 0.01
    
    balance = account_info.balance
    risk_amount = balance * risk_percentage
    
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        print("Failed to get symbol info")
        return 0.01
        
    point = symbol_info.point
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size
    
    if point == 0 or tick_size == 0 or tick_value == 0:
         return 0.01
         
    distance_in_points = abs(entry_price - sl_price) / point
    # Prevent division by zero if entry == sl
    if distance_in_points == 0:
        return 0.01

    loss_per_lot = distance_in_points * (tick_value / (tick_size / point))
    
    if loss_per_lot == 0:
        return 0.01
        
    lot_size = risk_amount / loss_per_lot
    
    # round to allowed step
    step = symbol_info.volume_step
    if step > 0:
        lot_size = math.floor(lot_size / step) * step
    
    # constrain to min/max
    if lot_size < symbol_info.volume_min:
        lot_size = symbol_info.volume_min
    if lot_size > symbol_info.volume_max:
        lot_size = symbol_info.volume_max
        
    return round(lot_size, 2)

def _execute_trade_sync(action, raw_symbol, sl, tp, lot_size=None, deviation=10):
    if not initialize_mt5():
        return {"success": False, "message": "Failed to connect to MT5 Desktop App. Is it running?"}
        
    # Clean symbol (e.g. AUDUSD=X -> AUDUSD)
    symbol = raw_symbol.replace('=X', '')
    
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        mt5.shutdown()
        return {"success": False, "message": f"Symbol {symbol} not found in MT5"}
        
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            mt5.shutdown()
            return {"success": False, "message": f"Failed to select symbol {symbol} in MT5 Market Watch"}
            
    # Get current market price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        mt5.shutdown()
        return {"success": False, "message": f"Failed to get current price for {symbol}"}

    if action.upper() == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif action.upper() == "SELL":
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        return {"success": False, "message": "Invalid action. Must be BUY or SELL"}
        
    if lot_size is None:
        lot_size = calculate_lot_size(symbol, price, float(sl), 0.01) # 1% risk fallback
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot_size),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": deviation,
        "magic": 123456,
        "comment": "Gemini Auto Execution",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = mt5.order_send(request)
    
    if result is None:
        mt5.shutdown()
        error_code = mt5.last_error()
        return {"success": False, "message": f"MT5 rejected the order request entirely. Check parameters. (Code: {error_code})"}
        
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        # Fallback to ORDER_FILLING_FOK if IOC is not supported by broker
        print(f"IOC failed with code {result.retcode}. Retrying with FOK...")
        request["type_filling"] = mt5.ORDER_FILLING_FOK
        result = mt5.order_send(request)
        
        if result is None:
            mt5.shutdown()
            return {"success": False, "message": "MT5 rejected the FOK fallback order request."}

    mt5.shutdown()
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"success": False, "message": f"MT5 Order failed: {result.comment} (Code: {result.retcode})"}
        
    return {
        "success": True, 
        "message": f"Successfully executed {action} for {symbol}",
        "ticket": result.order,
        "volume": result.volume,
        "price": result.price
    }

def execute_trade(action, raw_symbol, sl, tp, lot_size=None, deviation=10, timeout_sec=5.0):
    """
    Wrapper function to execute trades with Timeout Protection.
    Runs the MT5 order execution in a daemon thread so it doesn't freeze the FastAPI server.
    """
    result_container = {"success": False, "message": "Operation timed out."}
    
    def target():
        try:
            res = _execute_trade_sync(action, raw_symbol, sl, tp, lot_size, deviation)
            # Update container with the result from the sync function
            for key, value in res.items():
                result_container[key] = value
        except Exception as e:
            result_container["success"] = False
            result_container["message"] = f"Execution Thread Error: {str(e)}"
            
    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    
    # Wait for the thread to complete, up to timeout_sec
    thread.join(timeout_sec)
    
    if thread.is_alive():
        # The thread is still running, which means MT5 is hung.
        try:
            mt5.shutdown() # Attempt to gracefully close the connection
        except:
            pass
        return {"success": False, "message": f"MT5 Execution timed out after {timeout_sec}s. The terminal might be unresponsive."}
        
    return result_container

def get_account_analytics():
    if not initialize_mt5():
        return {"success": False, "message": "Failed to connect to MT5 Desktop App."}
        
    account_info = mt5.account_info()
    if account_info is None:
        mt5.shutdown()
        return {"success": False, "message": "Failed to get account info"}
        
    balance = account_info.balance
    
    # Get history deals
    # Use a large time frame, e.g., 30 days
    date_from = datetime.now() - timedelta(days=30)
    date_to = datetime.now() + timedelta(days=1)
    
    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        mt5.shutdown()
        return {"success": False, "message": "Failed to get history deals"}
        
    running_profit = 0.0
    peak_profit = 0.0
    max_drawdown = 0.0
    
    for deal in deals:
        if deal.profit != 0:
            running_profit += deal.profit
            if running_profit > peak_profit:
                peak_profit = running_profit
                
            drawdown = peak_profit - running_profit
            if drawdown > max_drawdown:
                max_drawdown = drawdown
                
    net_profit = running_profit
    
    recovery_factor = 0.0
    if max_drawdown > 0:
        recovery_factor = net_profit / max_drawdown
        
    mt5.shutdown()
    
    return {
        "success": True,
        "balance": balance,
        "net_profit": round(net_profit, 2),
        "max_drawdown": round(max_drawdown, 2),
        "recovery_factor": round(recovery_factor, 2)
    }

def apply_smart_trailing_stop(atr_multiplier=1.5):
    if not initialize_mt5():
        return {"success": False, "message": "Failed to connect to MT5 Desktop App."}
        
    positions = mt5.positions_get()
    if positions is None:
        mt5.shutdown()
        return {"success": False, "message": "Failed to get positions"}
        
    modifications = 0
    for pos in positions:
        symbol = pos.symbol
        ticket = pos.ticket
        pos_type = pos.type
        current_sl = pos.sl
        current_price = pos.price_current
        open_price = pos.price_open
        
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 14)
        if rates is None or len(rates) < 14:
            continue
            
        # Calculate ATR
        tr_list = []
        for i in range(1, len(rates)):
            high = rates[i]['high']
            low = rates[i]['low']
            prev_close = rates[i-1]['close']
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_list.append(tr)
            
        atr = sum(tr_list) / len(tr_list)
        trail_distance = atr * atr_multiplier
        
        new_sl = current_sl
        modified = False
        
        if pos_type == mt5.ORDER_TYPE_BUY:
            proposed_sl = current_price - trail_distance
            if current_price > open_price and proposed_sl > current_sl:
                new_sl = proposed_sl
                modified = True
        elif pos_type == mt5.ORDER_TYPE_SELL:
            proposed_sl = current_price + trail_distance
            if current_price < open_price and (current_sl == 0 or proposed_sl < current_sl):
                new_sl = proposed_sl
                modified = True
                
        if modified:
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info:
                 new_sl = round(new_sl, symbol_info.digits)
                 request = {
                     "action": mt5.TRADE_ACTION_SLTP,
                     "symbol": symbol,
                     "position": ticket,
                     "sl": float(new_sl),
                     "tp": float(pos.tp)
                 }
                 result = mt5.order_send(request)
                 if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                     modifications += 1

    mt5.shutdown()
    return {"success": True, "modifications": modifications}

def evaluate_ticket(ticket_id: int) -> str:
    """
    Checks MT5 natively to see if the trade won, lost, or is still pending.
    Returns: 'WON', 'LOST', 'PENDING', or 'NOT_FOUND'
    """
    if ticket_id <= 0:
        return 'NOT_FOUND'
        
    if not initialize_mt5():
        return 'PENDING' # Can't check now
        
    # Check if position is still open
    positions = mt5.positions_get(ticket=ticket_id)
    if positions is not None and len(positions) > 0:
        mt5.shutdown()
        return 'PENDING'
        
    # If not open, check history deals related to this position
    # The deal that closes the position has position_id == ticket_id
    from datetime import datetime, timedelta
    
    # Check history from a wide range (e.g. last 30 days) to find the close deal
    date_from = datetime.now() - timedelta(days=30)
    date_to = datetime.now() + timedelta(days=1)
    
    deals = mt5.history_deals_get(date_from, date_to, position=ticket_id)
    if deals is None or len(deals) == 0:
        mt5.shutdown()
        return 'NOT_FOUND' # Maybe it was never executed or too old
        
    # Find the closing deal (Entry == 1 which is DEAL_ENTRY_OUT)
    # If profit + swap + commission > 0 it's a win, else loss.
    total_profit = 0.0
    for deal in deals:
        if deal.entry == 1: # DEAL_ENTRY_OUT (Closing the trade)
            total_profit += deal.profit + deal.swap + deal.commission
            
    mt5.shutdown()
    
    if total_profit > 0:
        return 'WON'
    else:
        # Zero profit is considered a loss of opportunity/spread
        return 'LOST'
