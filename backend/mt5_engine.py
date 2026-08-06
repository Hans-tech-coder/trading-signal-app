import MetaTrader5 as mt5
import math
import threading
from datetime import datetime, timedelta

# Global ATR Cache to save CPU (Symbol -> ATR Value)
cached_atrs = {}

def update_atr_cache(symbols: list):
    """
    Updates the global ATR cache for the given symbols.
    This prevents the tick listener from heavily calculating ATR on every tick.
    """
    if not symbols:
        return
        
    if not initialize_mt5():
        return
        
    for symbol in symbols:
        try:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 14)
            if rates is None or len(rates) < 14:
                continue
            tr_list = []
            for i in range(1, len(rates)):
                high = rates[i]['high']
                low = rates[i]['low']
                prev_close = rates[i-1]['close']
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                tr_list.append(tr)
            atr = sum(tr_list) / len(tr_list)
            cached_atrs[symbol] = atr
        except Exception as e:
            print(f"Failed to update ATR cache for {symbol}: {e}")
            
    # mt5.shutdown() # Disabled to keep global connection alive

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

def _execute_trade_sync(action, raw_symbol, entry, sl, tp, lot_size=None, deviation=10):
    if not initialize_mt5():
        return {"success": False, "message": "Failed to connect to MT5 Desktop App. Is it running?"}
        
    # Clean symbol (e.g. AUDUSD=X -> AUDUSD)
    symbol = raw_symbol.replace('=X', '')
    
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        # mt5.shutdown() # Disabled to keep global connection alive
        return {"success": False, "message": f"Symbol {symbol} not found in MT5"}
        
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            # mt5.shutdown() # Disabled to keep global connection alive
            return {"success": False, "message": f"Failed to select symbol {symbol} in MT5 Market Watch"}
            
    # Get current market price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        # mt5.shutdown() # Disabled to keep global connection alive
        return {"success": False, "message": f"Failed to get current price for {symbol}"}

    if action.upper() == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif action.upper() == "SELL":
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    elif action.upper() == "BUY LIMIT":
        order_type = mt5.ORDER_TYPE_BUY_LIMIT
        price = float(entry) # Use the AI's requested entry price
    elif action.upper() == "SELL LIMIT":
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
        price = float(entry) # Use the AI's requested entry price
    else:
        return {"success": False, "message": "Invalid action. Must be BUY, SELL, BUY LIMIT, or SELL LIMIT"}
        
    if lot_size is None:
        lot_size = calculate_lot_size(symbol, price, float(sl), 0.01) # 1% risk fallback
    
    # Determine correct MT5 action type
    trade_action = mt5.TRADE_ACTION_DEAL
    if order_type in [mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT]:
        trade_action = mt5.TRADE_ACTION_PENDING

    request = {
        "action": trade_action,
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
        # mt5.shutdown() # Disabled to keep global connection alive
        error_code = mt5.last_error()
        return {"success": False, "message": f"MT5 rejected the order request entirely. Check parameters. (Code: {error_code})"}
        
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        # Fallback to ORDER_FILLING_FOK if IOC is not supported by broker
        print(f"IOC failed with code {result.retcode}. Retrying with FOK...")
        request["type_filling"] = mt5.ORDER_FILLING_FOK
        result = mt5.order_send(request)
        
        if result is None:
            # mt5.shutdown() # Disabled to keep global connection alive
            return {"success": False, "message": "MT5 rejected the FOK fallback order request."}

    # mt5.shutdown() # Disabled to keep global connection alive
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"success": False, "message": f"MT5 Order failed: {result.comment} (Code: {result.retcode})"}
        
    return {
        "success": True, 
        "message": f"Successfully executed {action} for {symbol}",
        "ticket": result.order,
        "volume": result.volume,
        "price": result.price
    }

def execute_trade(action, raw_symbol, entry, sl, tp, lot_size=None, deviation=10, timeout_sec=15.0):
    """
    Wrapper function to execute trades with Timeout Protection.
    Runs the MT5 order execution in a daemon thread so it doesn't freeze the FastAPI server.
    """
    result_container = {"success": False, "message": "Operation timed out."}
    
    def target():
        try:
            res = _execute_trade_sync(action, raw_symbol, entry, sl, tp, lot_size, deviation)
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
        # mt5.shutdown() # Disabled to keep global connection alive # Attempt to gracefully close the connection
        return {"success": False, "message": f"MT5 Execution timed out after {timeout_sec}s. The terminal might be unresponsive."}
        
    return result_container

def get_account_analytics():
    if not initialize_mt5():
        return {"success": False, "message": "Failed to connect to MT5 Desktop App."}
        
    account_info = mt5.account_info()
    if account_info is None:
        # mt5.shutdown() # Disabled to keep global connection alive
        return {"success": False, "message": "Failed to get account info"}
        
    balance = account_info.balance
    
    # Get history deals
    # Use a large time frame, e.g., 30 days
    date_from = datetime.now() - timedelta(days=30)
    date_to = datetime.now() + timedelta(days=1)
    
    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        # mt5.shutdown() # Disabled to keep global connection alive
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
        
    # mt5.shutdown() # Disabled to keep global connection alive
    
    return {
        "success": True,
        "balance": balance,
        "net_profit": round(net_profit, 2),
        "max_drawdown": round(max_drawdown, 2),
        "recovery_factor": round(recovery_factor, 2)
    }

def apply_tick_based_trailing_stop(symbol, current_price, atr_multiplier=1.0):
    if not initialize_mt5():
        return {"success": False, "message": "Failed to connect to MT5 Desktop App."}
        
    positions = mt5.positions_get(symbol=symbol)
    if positions is None or len(positions) == 0:
        # No mt5.shutdown() needed if called from tick_listener as it manages its own thread lifecycle or we can call it. Wait, initialize_mt5() calls mt5.initialize(), which is fine to keep open or shut down. Let's shut down just in case.
        # mt5.shutdown() # Disabled to keep global connection alive
        return {"success": True, "modifications": 0}
        
    modifications = 0
    for pos in positions:
        ticket = pos.ticket
        pos_type = pos.type
        current_sl = pos.sl
        open_price = pos.price_open
        
        atr = cached_atrs.get(symbol, None)
        if atr is None:
            # Fallback
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 14)
            if rates is not None and len(rates) >= 14:
                tr_list = []
                for i in range(1, len(rates)):
                    high = rates[i]['high']
                    low = rates[i]['low']
                    prev_close = rates[i-1]['close']
                    tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                    tr_list.append(tr)
                atr = sum(tr_list) / len(tr_list)
                
        if atr is None:
            continue
            
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

    # mt5.shutdown() # Disabled to keep global connection alive
    return {"success": True, "modifications": modifications}

def apply_smart_trailing_stop(atr_multiplier=1.0):
    if not initialize_mt5():
        return {"success": False, "message": "Failed to connect to MT5 Desktop App."}
        
    positions = mt5.positions_get()
    if positions is None:
        # mt5.shutdown() # Disabled to keep global connection alive
        return {"success": False, "message": "Failed to get positions"}
        
    modifications = 0
    for pos in positions:
        symbol = pos.symbol
        ticket = pos.ticket
        pos_type = pos.type
        current_sl = pos.sl
        current_price = pos.price_current
        open_price = pos.price_open
        
        # We replace the heavy on-the-fly calculation with the cached value
        atr = cached_atrs.get(symbol, None)
        if atr is None:
            # Fallback if cache is missed
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 14)
            if rates is None or len(rates) < 14:
                continue
            tr_list = []
            for i in range(1, len(rates)):
                high = rates[i]['high']
                low = rates[i]['low']
                prev_close = rates[i-1]['close']
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                tr_list.append(tr)
            atr = sum(tr_list) / len(tr_list)
            
        # Calculate minimum allowed SL distance based on cached ATR
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

    # mt5.shutdown() # Disabled to keep global connection alive
    return {"success": True, "modifications": modifications}

def run_trade_manager(active_trades):
    """
    Checks active trades against MT5. If an expiry time is met, closes the trade at market price.
    Returns the number of trades closed.
    """
    if not active_trades:
        return 0
        
    if not initialize_mt5():
        print("Trade Manager: MT5 connection failed.")
        return 0
        
    positions = mt5.positions_get()
    if positions is None:
        # mt5.shutdown() # Disabled to keep global connection alive
        return 0
        
    closed_count = 0
    now = datetime.now()
    
    for db_trade in active_trades:
        # Does the MT5 position still exist?
        pos = next((p for p in positions if p.ticket == db_trade["ticket_id"]), None)
        
        if pos:
            # Check expiry
            try:
                # Fallback to 12 hours if AI gave bad text or no expiry
                expiry_hours = 12
                if db_trade["expiry"]:
                    try:
                        expiry_hours = float(db_trade["expiry"])
                    except ValueError:
                        pass # Kept at 12
                        
                # db_trade["date"] format is currently "YYYY-MM-DD" from date_generated, but we need exact time
                # However, MT5 position has a exact time open
                time_open = datetime.fromtimestamp(pos.time)
                elapsed_hours = (now - time_open).total_seconds() / 3600.0
                
                if elapsed_hours >= expiry_hours:
                    print(f"Trade Manager: Auto-closing ticket {pos.ticket} (Elapsed {elapsed_hours:.2f}h >= {expiry_hours}h expiry)")
                    
                    # Close at market price
                    action_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                    tick = mt5.symbol_info_tick(pos.symbol)
                    price = tick.bid if action_type == mt5.ORDER_TYPE_SELL else tick.ask
                    
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": pos.symbol,
                        "volume": pos.volume,
                        "type": action_type,
                        "position": pos.ticket,
                        "price": price,
                        "deviation": 20,
                        "magic": 0,
                        "comment": "Auto-Expiry Close",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }
                    
                    res = mt5.order_send(request)
                    if res and res.retcode != mt5.TRADE_RETCODE_DONE:
                        # Fallback to ORDER_FILLING_FOK if IOC fails (e.g. Code 10030)
                        request["type_filling"] = mt5.ORDER_FILLING_FOK
                        res = mt5.order_send(request)
                        
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        print(f"Trade Manager: Closed ticket {pos.ticket} successfully.")
                        closed_count += 1
                        import database # Lazy import
                        database.update_trade_status(db_trade["id"], "EXPIRED/CLOSED")
                    else:
                        print(f"Trade Manager: Failed to close ticket {pos.ticket}. Code: {res.retcode if res else 'None'}")
                        
            except Exception as e:
                print(f"Trade Manager Error on active ticket {pos.ticket}: {e}")
                
        else:
            # Maybe it's a Pending Limit Order?
            orders = mt5.orders_get()
            if orders:
                pending_order = next((o for o in orders if o.ticket == db_trade["ticket_id"]), None)
                if pending_order:
                    try:
                        expiry_hours = 12
                        if db_trade["expiry"]:
                            try:
                                expiry_hours = float(db_trade["expiry"])
                            except ValueError:
                                pass
                                
                        time_setup = datetime.fromtimestamp(pending_order.time_setup)
                        elapsed_hours = (now - time_setup).total_seconds() / 3600.0
                        
                        if elapsed_hours >= expiry_hours:
                            print(f"Trade Manager: Auto-deleting expired Pending Order {pending_order.ticket}")
                            request = {
                                "action": mt5.TRADE_ACTION_REMOVE,
                                "order": pending_order.ticket
                            }
                            res = mt5.order_send(request)
                            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                                closed_count += 1
                                import database
                                database.update_trade_status(db_trade["id"], "EXPIRED/DELETED")
                    except Exception as e:
                        print(f"Trade Manager Error on pending ticket {pending_order.ticket}: {e}")
                        
    # mt5.shutdown() # Disabled to keep global connection alive
    return closed_count

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
        # mt5.shutdown() # Disabled to keep global connection alive
        return 'PENDING'
        
    # Check history to find the close deal
    deals = mt5.history_deals_get(position=ticket_id)
    if deals is None or len(deals) == 0:
        # mt5.shutdown() # Disabled to keep global connection alive
        return 'NOT_FOUND' # Maybe it was never executed or too old
        
    # Find the closing deal (Entry == 1 which is DEAL_ENTRY_OUT)
    # If profit + swap + commission > 0 it's a win, else loss.
    total_profit = 0.0
    for deal in deals:
        if deal.entry == 1: # DEAL_ENTRY_OUT (Closing the trade)
            total_profit += deal.profit + deal.swap + deal.commission
            
    # mt5.shutdown() # Disabled to keep global connection alive
    
    if total_profit > 0:
        return 'WON'
    else:
        # Zero profit is considered a loss of opportunity/spread
        return 'LOST'


def get_10_day_volatility(symbol: str) -> float:
    """
    Calculates the 10-day volatility move directly from MT5 historical data.
    Returns the absolute percentage move.
    """
    if not initialize_mt5():
        return 0.0

    # mt5.TIMEFRAME_D1 is daily candles.
    # copy_rates_from_pos gets the last N candles up to the current moment.
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 10)
    
    if rates is None or len(rates) < 2:
        return 0.0
        
    start_price = rates[0]['close']
    end_price = rates[-1]['close']
    
    if start_price == 0:
        return 0.0
        
    move = abs((end_price - start_price) / start_price)
    return move
