import MetaTrader5 as mt5
import math

def initialize_mt5():
    if not mt5.initialize():
        print("initialize() failed, error code =", mt5.last_error())
        return False
    return True

def calculate_lot_size(symbol, entry_price, sl_price, risk_percentage=0.01):
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

def execute_trade(action, raw_symbol, sl, tp, deviation=10):
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
        mt5.shutdown()
        return {"success": False, "message": "Invalid action. Must be BUY or SELL"}
        
    lot_size = calculate_lot_size(symbol, price, float(sl), 0.01) # 1% risk
    
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
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        # Fallback to ORDER_FILLING_FOK if IOC is not supported by broker
        print(f"IOC failed with code {result.retcode}. Retrying with FOK...")
        request["type_filling"] = mt5.ORDER_FILLING_FOK
        result = mt5.order_send(request)

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
