"""
BN Terminal Server — Upstox Edition (All-in-One)
==================================================
Terminal is served directly from this server.
No HTML file needed on your device.

After deploying to Railway:
1. Open https://your-railway-url.up.railway.app
2. Click Login with Upstox (one time)
3. Then open https://your-railway-url.up.railway.app/terminal
   on any device — phone, laptop, anywhere

SETUP:
  pip install flask flask-cors requests gunicorn
  python bn_server.py
"""

import threading, time, os, requests, json, struct, queue
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, redirect, request, Response, stream_with_context
from flask_cors import CORS
from urllib.parse import urlencode
try:
    import websocket  # websocket-client library
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    print("[WS] websocket-client not installed, falling back to REST polling")

app = Flask(__name__)
CORS(app)

API_KEY      = os.environ.get("UPSTOX_API_KEY",    "2ea6b52c-e87b-4ae0-b84c-6395a73790a2")
API_SECRET   = os.environ.get("UPSTOX_API_SECRET", "urcjw710uv")
REDIRECT_URI = os.environ.get("REDIRECT_URI",      "http://127.0.0.1:5000/callback")
TOKEN_FILE   = "upstox_token.json"
BN_KEY       = "NSE_INDEX|Nifty Bank"
VIX_KEY      = "NSE_INDEX|India VIX"

state = {"access_token": ""}
cache = {
    "spot": 0, "change": 0, "pct": 0,
    "high": 0, "low": 0, "open": 0, "vwap": 0,
    "vix": 0, "pcr": 0, "max_pain": 0,
    "tot_ce_oi": 0, "tot_pe_oi": 0,
    "sp500_chg": 0, "crude_chg": 0, "gold_chg": 0, "usdinr": 0,
    "option_chain": [],
    "last_updated": "", "source": "starting",
    "error": "", "authenticated": False,
    "market_open": False,
    "last_session": {},  # stores last known good market data
}
LAST_SESSION_FILE = "last_session.json"
TRADES_FILE = "paper_trades.json"
JSONBIN_KEY = os.environ.get("JSONBIN_KEY", "")
JSONBIN_BIN_ID = os.environ.get("JSONBIN_BIN_ID", "")

def save_to_cloud(data):
    """Save trades to JSONBin.io for persistence across restarts."""
    if not JSONBIN_KEY or not JSONBIN_BIN_ID:
        return False
    try:
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
        r = requests.put(url, json=data,
            headers={"Content-Type":"application/json","X-Master-Key":JSONBIN_KEY},
            timeout=10)
        if r.status_code == 200:
            print("[CLOUD] Trades saved to cloud")
            return True
    except Exception as e:
        print(f"[CLOUD] Save error: {e}")
    return False

def load_from_cloud():
    """Load trades from JSONBin.io on server start."""
    if not JSONBIN_KEY or not JSONBIN_BIN_ID:
        return None
    try:
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
        r = requests.get(url,
            headers={"X-Master-Key":JSONBIN_KEY},
            timeout=10)
        if r.status_code == 200:
            data = r.json().get("record", {})
            print(f"[CLOUD] Trades loaded from cloud: {data.get('stats',{}).get('total',0)} trades")
            return data
    except Exception as e:
        print(f"[CLOUD] Load error: {e}")
    return None

# ═══════════════════ PHASE 2 — END OF DAY LEARNING ═══════════════════
LEARNING_FILE = "learning_log.json"
learning = {"days": [], "signal_stats": {}, "pattern_weights": {}}


# ═══════════════════ PHASE 3 — ADAPTIVE SIGNAL WEIGHTS ═══════════════════
def get_adaptive_confidence_boost(otype, vix):
    """
    Phase 3: Return confidence adjustment based on learned patterns.
    Patterns that have been working get a boost.
    Patterns that have been losing get penalized.
    """
    vix_bucket = "high" if vix > 22 else "normal"
    # Check if we have learned data for this pattern
    # Use day_direction from cache if available
    day_move = cache.get("spot", 0) - cache.get("open", 0)
    day_direction = "UP" if day_move > 50 else "DOWN" if day_move < -50 else "FLAT"
    direction_match = (otype=="CE" and day_direction=="UP") or (otype=="PE" and day_direction=="DOWN")
    pattern_key = f"{vix_bucket}_{otype}_{direction_match}"
    weight = learning["pattern_weights"].get(pattern_key, 1.0)
    # Convert weight to confidence boost (-15 to +15)
    boost = round((weight - 1.0) * 30)
    boost = max(-15, min(15, boost))
    if boost != 0:
        print(f"[PHASE3] Pattern {pattern_key} weight={weight:.2f} boost={boost:+d}%")
    return boost

def load_learning():
    try:
        with open(LEARNING_FILE) as f:
            data = json.load(f)
            learning["days"] = data.get("days", [])
            learning["signal_stats"] = data.get("signal_stats", {})
            learning["pattern_weights"] = data.get("pattern_weights", {})
            print(f"[LEARN] Loaded {len(learning['days'])} days of learning data")
    except: pass

def save_learning():
    try:
        with open(LEARNING_FILE, "w") as f:
            json.dump(learning, f, indent=2)
    except Exception as e:
        print(f"[LEARN] Save error: {e}")

def end_of_day_review():
    """Run at market close — analyze today's trades and learn."""
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(timezone.utc).astimezone(ist).strftime("%Y-%m-%d")
    
    trades_today = [t for t in paper["trades"] 
                   if t.get("time","").startswith(datetime.now(timezone.utc).astimezone(ist).strftime("%d %b"))]
    
    if not trades_today:
        print("[LEARN] No trades today to review")
        return
    
    # Calculate day stats
    total = len(trades_today)
    wins = sum(1 for t in trades_today if t.get("pnl", 0) > 0)
    losses = total - wins
    total_pnl = sum(t.get("pnl", 0) for t in trades_today)
    win_rate = round(wins/total*100, 1) if total > 0 else 0
    avg_win = round(sum(t["pnl"] for t in trades_today if t.get("pnl",0)>0) / max(wins,1), 0)
    avg_loss = round(sum(t["pnl"] for t in trades_today if t.get("pnl",0)<0) / max(losses,1), 0)
    
    # Market conditions
    vix_today = cache.get("vix", 0)
    spot_open = cache.get("open", 0)
    spot_close = cache.get("spot", 0)
    day_move = round(spot_close - spot_open, 0) if spot_open else 0
    day_direction = "UP" if day_move > 0 else "DOWN" if day_move < 0 else "FLAT"
    
    # Analyze each trade — was signal direction correct?
    for t in trades_today:
        signal_type = t.get("otype", "")
        correct_direction = (signal_type == "CE" and day_direction == "UP") or                            (signal_type == "PE" and day_direction == "DOWN")
        t["direction_correct"] = correct_direction
        t["exit_reason"] = t.get("reason", "UNKNOWN")
        
        # Update signal stats
        key = f"{signal_type}_{t.get('exit_reason','')}"
        if key not in learning["signal_stats"]:
            learning["signal_stats"][key] = {"count":0,"wins":0,"total_pnl":0}
        learning["signal_stats"][key]["count"] += 1
        if t.get("pnl",0) > 0:
            learning["signal_stats"][key]["wins"] += 1
        learning["signal_stats"][key]["total_pnl"] += t.get("pnl",0)
    
    # Build day record
    day_record = {
        "date": today,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "vix": vix_today,
        "day_move_pts": day_move,
        "day_direction": day_direction,
        "trade_details": [{
            "otype": t.get("otype"),
            "pnl": t.get("pnl"),
            "exit_reason": t.get("reason"),
            "direction_correct": t.get("direction_correct"),
            "vix_at_entry": t.get("vix_at_entry")
        } for t in trades_today],
        "lessons": generate_lessons(trades_today, day_direction, vix_today)
    }
    
    learning["days"].insert(0, day_record)
    if len(learning["days"]) > 90:  # keep 90 days
        learning["days"] = learning["days"][:90]
    
    # Update pattern weights based on performance
    update_pattern_weights(trades_today, vix_today, day_direction)
    
    save_learning()
    print(f"[LEARN] Day review: {total} trades | {win_rate}% WR | P&L ₹{total_pnl:+,.0f}")
    print(f"[LEARN] Lessons: {day_record['lessons']}")
    return day_record

def generate_lessons(trades, day_direction, vix):
    """Generate human-readable lessons from today's trades."""
    lessons = []
    wins = [t for t in trades if t.get("pnl",0) > 0]
    losses = [t for t in trades if t.get("pnl",0) < 0]
    
    # Lesson 1: Direction accuracy
    correct = sum(1 for t in trades if t.get("direction_correct"))
    if correct == len(trades):
        lessons.append("All signals correct direction today")
    elif correct == 0:
        lessons.append(f"All signals wrong direction — market was {day_direction}, need to improve trend detection")
    else:
        lessons.append(f"{correct}/{len(trades)} signals correct direction")
    
    # Lesson 2: Exit quality
    sl_hits = sum(1 for t in losses if "STOP LOSS" in t.get("reason",""))
    if sl_hits > 1:
        lessons.append(f"SL hit {sl_hits} times — consider wider stops on VIX {vix:.0f} days")
    
    early_exits = sum(1 for t in wins if "RSI" in t.get("reason","") or "TRAIL" in t.get("reason",""))
    if early_exits > 0:
        lessons.append(f"Brain exited {early_exits} trade(s) early — check if more profit was available")
    
    # Lesson 3: VIX context
    if vix > 22:
        lessons.append(f"High VIX day ({vix:.1f}) — volatile conditions, wider SL needed")
    
    return lessons

def update_pattern_weights(trades, vix, day_direction):
    """Adjust signal weights based on what worked today."""
    for t in trades:
        otype = t.get("otype","")
        exit_reason = t.get("reason","")
        pnl = t.get("pnl",0)
        vix_bucket = "high" if vix>22 else "normal"
        
        # Build pattern key
        direction_match = (otype=="CE" and day_direction=="UP") or (otype=="PE" and day_direction=="DOWN")
        pattern_key = f"{vix_bucket}_{otype}_{direction_match}"
        
        if pattern_key not in learning["pattern_weights"]:
            learning["pattern_weights"][pattern_key] = 1.0
        
        # Winning trades increase weight, losing trades decrease
        if pnl > 0:
            learning["pattern_weights"][pattern_key] = min(2.0, 
                learning["pattern_weights"][pattern_key] * 1.05)
        else:
            learning["pattern_weights"][pattern_key] = max(0.3,
                learning["pattern_weights"][pattern_key] * 0.95)

def get_learning_summary():
    """Get summary of learning for API."""
    if not learning["days"]:
        return {"status": "No data yet", "days_recorded": 0}
    
    days = learning["days"]
    total_trades = sum(d["trades"] for d in days)
    total_wins = sum(d["wins"] for d in days)
    total_pnl = sum(d["total_pnl"] for d in days)
    overall_wr = round(total_wins/total_trades*100, 1) if total_trades > 0 else 0
    
    # Best and worst patterns
    best_patterns = sorted(
        [(k,v) for k,v in learning["pattern_weights"].items() if v > 1.0],
        key=lambda x: x[1], reverse=True
    )[:3]
    worst_patterns = sorted(
        [(k,v) for k,v in learning["pattern_weights"].items() if v < 1.0],
        key=lambda x: x[1]
    )[:3]
    
    return {
        "status": "Active",
        "days_recorded": len(days),
        "total_trades": total_trades,
        "overall_win_rate": overall_wr,
        "total_pnl": total_pnl,
        "recent_7_days": days[:7],
        "best_patterns": best_patterns,
        "worst_patterns": worst_patterns,
        "signal_stats": learning["signal_stats"],
        "latest_lessons": days[0]["lessons"] if days else []
    }


# Paper trading state
paper = {
    "capital": 100000,
    "available": 100000,
    "open_trade": None,
    "trades": [],
    "stats": {"total": 0, "wins": 0, "losses": 0, "pnl": 0},
    "daily": {"date": "", "trades": 0, "pnl": 0, "last_trade_time": 0}
}

# Trading rules
RULES = {
    "max_trades_per_day": 20,      # effectively unlimited - signal based
    "max_daily_loss_pct": 2.0,     # stop if 2% of capital lost in a day
    "no_trade_before": (9, 20),    # 9:20 AM IST
    "no_trade_after": (15, 0),     # 3:00 PM IST
    "min_gap_minutes": 5,   # 5 min gap on trending days         # 10 min gap between trades
    "min_confidence": 55,
    "max_daily_loss": 2000,        # ₹2000 max daily loss (2% of 1L)
}

def load_trades():
    # Try cloud first (survives Railway restarts)
    cloud_data = load_from_cloud()
    if cloud_data:
        paper["trades"] = cloud_data.get("trades", [])
        paper["stats"] = cloud_data.get("stats", {"total":0,"wins":0,"losses":0,"pnl":0})
        paper["available"] = cloud_data.get("available", 100000)
        paper["open_trade"] = cloud_data.get("open_trade", None)
        paper["capital"] = cloud_data.get("capital", 100000)
        print(f"[TRADES] Loaded {len(paper['trades'])} trades from cloud")
        return
    # Fallback to local file
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
            paper["trades"] = data.get("trades", [])
            paper["stats"] = data.get("stats", {"total":0,"wins":0,"losses":0,"pnl":0})
            paper["available"] = data.get("available", 100000)
            paper["open_trade"] = data.get("open_trade", None)
            print(f"[TRADES] Loaded {len(paper['trades'])} trades from local")
    except: pass

def save_trades():
    data = {"trades": paper["trades"], "stats": paper["stats"],
            "available": paper["available"], "open_trade": paper["open_trade"],
            "capital": paper["capital"]}
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[TRADES] Save error: {e}")
    # Also save to cloud for persistence
    threading.Thread(target=save_to_cloud, args=(data,), daemon=True).start()

def estimate_premium(spot, strike, otype, vix):
    """
    Realistic BN option premium estimator.
    Based on actual market observation:
    - ATM options trade at roughly VIX/100 * spot * sqrt(DTE/365)
    - Weekly expiry ~5 days out
    - OTM drops exponentially
    """
    import math
    vix_val = max(vix or 15, 12)
    vol = vix_val / 100
    DTE = 5 / 365  # weekly expiry ~5 days
    diff = abs(spot - strike)
    intrinsic = max(0, spot - strike) if otype == "CE" else max(0, strike - spot)
    # Time value using simplified BSM
    d = diff / (spot * vol * math.sqrt(DTE) + 1)
    time_val = spot * vol * math.sqrt(DTE) * math.exp(-0.5 * d * d) * 0.4
    premium = intrinsic + time_val
    # Realistic bounds for BN options
    # ATM should be ~100-400 depending on VIX
    # OTM 200pts away should be ~20-80
    premium = max(5.0, min(premium, 800.0))
    return round(premium, 2)

def check_exit(spot):
    """Check if open trade should be exited using VIX-adjusted SL."""
    t = paper["open_trade"]
    if not t: return
    otype = t["otype"]
    entry_spot = t["entry_spot"]
    t1 = t["t1"]
    t2 = t["t2"]
    # Use VIX-adjusted SL width from trade record
    sl_width = t.get("sl_width", 150)
    vix = t.get("vix_at_entry", cache.get("vix", 20))
    # Recalculate SL based on entry spot (not strike-based)
    if otype == "CE":
        sl = entry_spot - sl_width
    else:
        sl = entry_spot + sl_width
    # Check exits
    hit_sl = (otype == "CE" and spot <= sl) or (otype == "PE" and spot >= sl)
    hit_t1 = (otype == "CE" and spot >= t1) or (otype == "PE" and spot <= t1)
    hit_t2 = (otype == "CE" and spot >= t2) or (otype == "PE" and spot <= t2)
    reason = None
    if hit_t2: reason = "TARGET 2 HIT"
    elif hit_t1: reason = "TARGET 1 HIT"
    elif hit_sl: reason = f"STOP LOSS HIT ({sl_width}pt SL)"
    if reason:
        close_trade(spot, reason)

def open_trade(signal, spot, vix):
    """Open a new paper trade based on signal."""
    if paper["open_trade"]: return  # already in trade

    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(timezone.utc).astimezone(ist)
    today_str = now.strftime("%Y-%m-%d")

    # Reset daily counters if new day
    if paper["daily"]["date"] != today_str:
        paper["daily"] = {"date": today_str, "trades": 0, "pnl": 0, "last_trade_time": 0}

    # Signal-based trading — no fixed limit, just daily loss protection
    max_loss = paper["capital"] * (RULES.get("max_daily_loss_pct", 2.0) / 100)
    if paper["daily"]["pnl"] <= -max_loss:
        print(f"[PAPER] Daily loss limit reached — no more trades today")
        return

    # Rule: max daily loss - 2% of capital
    max_loss = paper["capital"] * (RULES.get("max_daily_loss_pct", 2.0) / 100)
    if paper["daily"]["pnl"] <= -max_loss:
        print(f"[PAPER] Daily loss limit hit: ₹{paper['daily']['pnl']:,.0f} / -₹{max_loss:.0f} — stopping for today")
        return

    # Rule: trading hours
    cur_mins = now.hour * 60 + now.minute
    start_mins = RULES["no_trade_before"][0] * 60 + RULES["no_trade_before"][1]
    end_mins = RULES["no_trade_after"][0] * 60 + RULES["no_trade_after"][1]
    if cur_mins < start_mins:
        print(f"[PAPER] Too early — before {RULES['no_trade_before'][0]}:{RULES['no_trade_before'][1]:02d} IST")
        return
    if cur_mins > end_mins:
        print(f"[PAPER] Too late — after {RULES['no_trade_after'][0]}:{RULES['no_trade_after'][1]:02d} IST")
        return

    # Rule: min gap between trades
    elapsed = time.time() - paper["daily"]["last_trade_time"]
    if paper["daily"]["last_trade_time"] > 0 and elapsed < RULES["min_gap_minutes"] * 60:
        wait = int((RULES["min_gap_minutes"] * 60 - elapsed) / 60)
        print(f"[PAPER] Too soon — wait {wait} more minutes")
        return
    otype = signal.get("otype", "CE")
    strike = signal.get("strike", round(spot/100)*100)
    sl = signal.get("sl", 0)
    t1 = signal.get("t1", 0)
    t2 = signal.get("t2", 0)
    if not sl or not t1: return
    # VIX-adjusted position sizing
    # High VIX = smaller position, low VIX = normal position
    # VIX-adjusted position sizing
    vix_factor = 1.0
    if vix > 25: vix_factor = 0.5
    elif vix > 20: vix_factor = 0.7
    elif vix < 14: vix_factor = 1.2

    # VIX-adjusted stop loss width (wider SL on high VIX days)
    # High VIX = market is noisy = need wider SL to survive intraday swings
    sl_width = 120  # default 120 points SL
    if vix > 25: sl_width = 200   # very volatile - 200pt SL
    elif vix > 20: sl_width = 160  # elevated - 160pt SL
    elif vix < 14: sl_width = 80   # calm - tighter 80pt SL

    # Override SL from signal with VIX-adjusted SL
    if otype == "CE":
        sl = spot - sl_width  # for calls, SL is below entry spot
    else:
        sl = spot + sl_width  # for puts, SL is above entry spot

    premium = estimate_premium(spot, strike, otype, vix)
    sl_premium = estimate_premium(sl, strike, otype, vix)
    risk_per_lot = abs(premium - sl_premium) * 15
    if risk_per_lot <= 0: risk_per_lot = premium * 0.3 * 15
    max_risk = paper["capital"] * 0.01 * vix_factor
    lots = max(1, int(max_risk / risk_per_lot)) if risk_per_lot > 0 else 1
    lots = min(lots, 3)  # cap at 3 lots for safety
    print(f"[PAPER] VIX={vix} SL_width={sl_width}pts premium=₹{premium} risk_per_lot=₹{risk_per_lot:.0f} lots={lots}")
    cost = premium * 15 * lots
    if cost > paper["available"]: return  # not enough capital
    trade = {
        "id": len(paper["trades"]) + 1,
        "time": datetime.now().strftime("%d %b %H:%M"),
        "signal": "BUY " + otype,
        "strike": strike,
        "otype": otype,
        "entry_spot": spot,
        "entry_premium": premium,
        "sl": sl,
        "t1": t1,
        "t2": t2,
        "sl_width": sl_width,
        "vix_at_entry": vix,
        "lots": lots,
        "qty": lots * 15,
        "cost": round(cost, 2),
        "status": "OPEN",
        "exit_spot": None,
        "exit_premium": None,
        "pnl": 0,
        "reason": None
    }
    paper["open_trade"] = trade
    paper["available"] -= cost
    paper["daily"]["trades"] += 1
    paper["daily"]["last_trade_time"] = time.time()
    print(f"[PAPER] Opened: {trade['signal']} {strike} @ ₹{premium} x {lots} lots | Trade {paper['daily']['trades']}/{RULES['max_trades_per_day']} today")
    save_trades()

def close_trade(exit_spot, reason):
    """Close the open paper trade."""
    t = paper["open_trade"]
    if not t: return
    exit_premium = estimate_premium(exit_spot, t["strike"], t["otype"], cache.get("vix", 15))
    pnl = round((exit_premium - t["entry_premium"]) * t["qty"], 2)
    t.update({"status": "CLOSED", "exit_spot": exit_spot,
               "exit_premium": exit_premium, "pnl": pnl, "reason": reason,
               "exit_time": datetime.now().strftime("%d %b %H:%M")})
    paper["trades"].insert(0, t)
    paper["open_trade"] = None
    paper["available"] += t["cost"] + pnl
    paper["stats"]["total"] += 1
    paper["stats"]["pnl"] = round(paper["stats"]["pnl"] + pnl, 2)
    if pnl >= 0: paper["stats"]["wins"] += 1
    else: paper["stats"]["losses"] += 1
    paper["daily"]["pnl"] = round(paper["daily"]["pnl"] + pnl, 2)
    print(f"[PAPER] Closed: {reason} | P&L ₹{pnl:+,.0f} | Daily P&L ₹{paper['daily']['pnl']:+,.0f}")
    save_trades()

# WebSocket state
ws_state = {
    "connected": False,
    "last_tick": 0,
    "reconnect_count": 0,
}
# SSE clients queue - each connected browser gets updates
sse_clients = []
sse_lock = threading.Lock()

def broadcast_price():
    """Push latest price to all SSE clients."""
    data = json.dumps({
        "spot": cache["spot"],
        "change": cache["change"],
        "pct": cache["pct"],
        "high": cache["high"],
        "low": cache["low"],
        "open": cache["open"],
        "vwap": cache["vwap"],
        "vix": cache["vix"],
        "pcr": cache["pcr"],
        "max_pain": cache["max_pain"],
        "sp500_chg": cache["sp500_chg"],
        "crude_chg": cache["crude_chg"],
        "gold_chg": cache["gold_chg"],
        "usdinr": cache["usdinr"],
        "last_updated": cache["last_updated"],
        "source": cache["source"],
        "market_open": cache.get("market_open", False),
        "ws_connected": ws_state["connected"],
    })
    msg = f"data: {data}\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(msg)
            except:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)

def save_last_session():
    """Save last good data to disk so it survives server restarts."""
    try:
        session = {k: cache[k] for k in [
            "spot","change","pct","high","low","open","vwap",
            "vix","pcr","max_pain","tot_ce_oi","tot_pe_oi",
            "sp500_chg","crude_chg","gold_chg","usdinr","option_chain","last_updated"
        ]}
        session["saved_at"] = datetime.now().strftime("%d %b %Y %H:%M IST")
        with open(LAST_SESSION_FILE, "w") as f:
            json.dump(session, f)
        cache["last_session"] = session
        print(f"[SESSION] Saved last session data.")
    except Exception as e:
        print(f"[SESSION] Save error: {e}")

def load_last_session():
    """Load last session data from disk."""
    try:
        with open(LAST_SESSION_FILE) as f:
            session = json.load(f)
            cache["last_session"] = session
            print(f"[SESSION] Loaded last session from {session.get('saved_at','?')}")
    except:
        pass

def is_market_open():
    """Check if NSE market is currently open (IST = UTC+5:30)."""
    # Use UTC time and add 330 minutes (5h30m) for IST
    utc_now = datetime.utcnow()
    ist_minutes = utc_now.hour * 60 + utc_now.minute + 330
    ist_hour = (ist_minutes // 60) % 24
    ist_min = ist_minutes % 60
    # Get IST weekday (handle day rollover)
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(timezone.utc).astimezone(ist)
    weekday = ist_now.weekday()
    if weekday >= 5:  # Saturday or Sunday
        print(f"[MARKET] Weekend — closed")
        return False
    cur = ist_hour * 60 + ist_min
    open_ok = (9 * 60 + 15) <= cur <= (15 * 60 + 30)
    print(f"[MARKET] IST={ist_hour:02d}:{ist_min:02d} weekday={weekday} open={open_ok}")
    return open_ok

TERMINAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0"/>
<title>BN Terminal · Live</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;800&family=Barlow+Condensed:wght@700;800;900&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#060A10;--bg2:#0A1018;--bg3:#0E1620;
  --bdr:#192336;--bdr2:#1F2E42;
  --orange:#FF6D00;--teal:#00BFA5;--green:#00E676;
  --red:#FF1744;--yellow:#FFD600;
  --white:#D8E8F8;--muted:#4A6070;--dim:#253545;
  --mono:'JetBrains Mono',monospace;--cond:'Barlow Condensed',sans-serif;
}
html,body{width:100%;height:100%;background:var(--bg);color:var(--white);font-family:var(--mono);font-size:12px;overflow:hidden}
#app{display:flex;flex-direction:column;height:100dvh}

/* SETUP SCREEN */
#setup{position:fixed;top:0;left:0;right:0;bottom:0;background:var(--bg);z-index:300;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;text-align:center}
#setup.hide{display:none}
.setup-box{background:var(--bg2);border:1px solid var(--bdr2);border-radius:8px;padding:24px 20px;max-width:380px;width:100%}
.setup-title{font-family:var(--cond);font-size:24px;font-weight:900;color:var(--orange);margin-bottom:6px;letter-spacing:0.08em}
.setup-sub{font-size:9px;color:var(--muted);margin-bottom:18px;line-height:1.6}
.setup-label{font-size:8px;color:var(--muted);letter-spacing:0.12em;text-transform:uppercase;text-align:left;margin-bottom:6px}
#server-url{width:100%;background:var(--bg3);border:2px solid var(--bdr2);color:var(--white);padding:10px 12px;font-family:var(--mono);font-size:11px;border-radius:4px;outline:none;margin-bottom:10px}
#server-url:focus{border-color:var(--orange)}
.setup-btn{width:100%;padding:12px;background:var(--orange);color:#000;border:none;font-family:var(--cond);font-size:16px;font-weight:900;cursor:pointer;border-radius:4px;letter-spacing:0.08em;margin-bottom:8px}
.setup-btn:hover{background:var(--yellow)}
.setup-hint{font-size:8px;color:var(--dim);line-height:1.7}
#test-result{font-size:9px;margin-top:8px;padding:6px 10px;border-radius:3px;display:none}

/* TOP BAR */
#topbar{display:flex;align-items:center;justify-content:space-between;padding:0 14px;height:52px;background:var(--bg2);border-bottom:2px solid var(--bdr2);flex-shrink:0}
.logo{display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--orange);box-shadow:0 0 10px var(--orange);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 6px var(--orange)}50%{box-shadow:0 0 20px var(--orange)}}
.logo-name{font-family:var(--cond);font-size:19px;font-weight:900;letter-spacing:0.1em}
.logo-sub{font-size:7px;color:var(--muted);letter-spacing:0.15em;text-transform:uppercase;margin-top:1px}
.top-right{display:flex;align-items:center;gap:8px}
#conn-pill{font-size:8px;font-weight:800;padding:3px 9px;border-radius:10px;border:1px solid var(--dim);color:var(--muted);display:flex;align-items:center;gap:4px;cursor:pointer}
#conn-pill .bd{width:6px;height:6px;border-radius:50%;animation:blink 1.4s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.2}}
#clock{font-family:var(--cond);font-size:14px;font-weight:800;color:var(--teal)}

/* PRICE STRIP */
#price-strip{display:flex;align-items:center;height:52px;background:var(--bg3);border-bottom:1px solid var(--bdr2);flex-shrink:0;padding:0 14px;gap:16px}
#spot-big{font-family:var(--cond);font-size:32px;font-weight:900;color:var(--muted);flex-shrink:0}
#chg{font-size:11px;font-weight:700;color:var(--muted);flex-shrink:0}
.ohlc{display:flex;gap:14px}
.oi{display:flex;flex-direction:column;gap:1px}
.oi-l{font-size:7px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase}
.oi-v{font-size:10px;font-weight:700}
#upd-info{margin-left:auto;font-size:8px;color:var(--muted);text-align:right;flex-shrink:0}

/* GLOBALS */
#globals{display:flex;align-items:center;overflow-x:auto;height:30px;background:var(--bg2);border-bottom:1px solid var(--bdr2);flex-shrink:0}
#globals::-webkit-scrollbar{display:none}
.g{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:0 10px;border-right:1px solid var(--bdr);flex-shrink:0;height:100%}
.g-l{font-size:6px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase}
.g-v{font-size:10px;font-weight:700;margin-top:1px}

/* TABS */
#tabs{display:flex;height:42px;background:var(--bg2);border-bottom:2px solid var(--bdr2);flex-shrink:0}
.tab{flex:1;display:flex;align-items:center;justify-content:center;font-family:var(--cond);font-size:13px;font-weight:800;letter-spacing:0.08em;color:var(--muted);border-right:1px solid var(--bdr);cursor:pointer;transition:all 0.15s;text-transform:uppercase}
.tab:last-child{border-right:none}
.tab.on{color:var(--orange);border-bottom:2px solid var(--orange);background:rgba(255,109,0,0.06)}

/* PAGES */
#pages{flex:1;min-height:0;overflow:hidden;position:relative}
.page{position:absolute;top:0;left:0;right:0;bottom:0;overflow-y:auto;padding:10px;display:none;flex-direction:column;gap:8px}
.page.on{display:flex}
.page::-webkit-scrollbar{width:3px}
.page::-webkit-scrollbar-track{background:var(--bg)}
.page::-webkit-scrollbar-thumb{background:var(--bdr2);border-radius:2px}

/* CARDS */
.card{background:var(--bg2);border:1px solid var(--bdr);border-radius:5px;overflow:hidden}
.card-hd{padding:7px 12px;background:var(--bg3);border-bottom:1px solid var(--bdr);font-size:8px;font-weight:700;letter-spacing:0.16em;color:var(--muted);text-transform:uppercase;display:flex;align-items:center;justify-content:space-between}
.sec-title{font-size:8px;font-weight:700;letter-spacing:0.16em;color:var(--muted);text-transform:uppercase;margin-bottom:6px}

/* SIGNAL */
.sig-ac{padding:18px 14px 12px;text-align:center;border-bottom:1px solid var(--bdr)}
.sv{font-family:var(--cond);font-size:52px;font-weight:900;letter-spacing:0.06em;line-height:1}
.ss{font-size:13px;font-weight:800;letter-spacing:0.12em;margin-top:4px}
.sb{font-family:var(--cond);font-size:20px;font-weight:800;padding:6px 16px;border-radius:3px;display:inline-block;margin-top:8px}
.lg{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--bdr)}
.lc{background:var(--bg2);padding:12px 14px;border-left:3px solid transparent}
.lc-l{font-size:7px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase}
.lc-v{font-size:20px;font-weight:900;font-family:var(--cond);margin-top:3px}
.cr{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--bdr)}
.cb{flex:1;height:10px;background:var(--bdr);border-radius:5px;overflow:hidden}
.cf{height:100%;border-radius:5px;transition:width 0.5s}
.cds{padding:10px 14px;display:flex;flex-wrap:wrap;gap:4px;border-bottom:1px solid var(--bdr)}
.cd{font-size:8px;font-weight:700;padding:3px 7px;border-radius:2px}
.sig-foot{padding:8px 14px;font-size:8px;color:var(--muted)}
.sw{padding:28px 14px;text-align:center}
.sw-t{font-family:var(--cond);font-size:34px;font-weight:900;color:var(--muted);margin:8px 0}
.sw-m{font-size:10px;color:var(--muted);line-height:1.7}

/* TREND */
.tlabel{font-family:var(--cond);font-size:26px;font-weight:900;letter-spacing:0.1em}
.tbar{height:8px;background:var(--bdr);border-radius:4px;overflow:hidden;margin:8px 0 6px}
.tbf{height:100%;border-radius:4px;transition:width 0.5s}
.tstrat{font-size:9px;font-weight:700;padding:5px 10px;border-radius:3px;display:inline-block;margin-top:4px;letter-spacing:0.05em}

/* INDS */
.ig{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.ind{background:var(--bg2);border:1px solid var(--bdr);border-radius:4px;padding:8px 10px}
.ind-l{font-size:7px;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase}
.ind-v{font-size:14px;font-weight:800;margin-top:3px;font-family:var(--cond)}
.ind-b{height:3px;background:var(--bdr);border-radius:2px;margin-top:4px;overflow:hidden}
.ind-bf{height:100%;border-radius:2px}

/* MINIS */
.mg{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.mi{background:var(--bg2);border:1px solid var(--bdr);border-radius:5px;padding:10px 12px}
.mi-l{font-size:7px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:4px}
.mi-v{font-size:20px;font-weight:900;font-family:var(--cond)}
.mi-s{font-size:8px;color:var(--muted);margin-top:2px}

/* SR */
.sr-list{display:flex;flex-direction:column;gap:3px}
.sr-row{display:flex;align-items:center;justify-content:space-between;padding:9px 12px;background:var(--bg2);border:1px solid var(--bdr);border-radius:4px}
.sr-n{font-size:9px;font-weight:700;letter-spacing:0.1em;color:var(--muted);width:36px}
.sr-v{font-size:15px;font-weight:800;font-family:var(--cond)}
.sr-d{font-size:9px;width:52px;text-align:right}
.sr-spot{background:var(--bg3);border-color:var(--bdr2)}

/* OC */
.oc-scroll{overflow-x:auto}
#oct{width:100%;border-collapse:collapse;min-width:320px}
#oct th{padding:6px 8px;font-size:7px;color:var(--muted);font-weight:700;letter-spacing:0.1em;text-align:right;border-bottom:1px solid var(--bdr);background:var(--bg3)}
#oct th.c{text-align:center;color:var(--yellow)}
#oct td{padding:4px 8px;font-size:10px;font-weight:600;text-align:right;border-bottom:1px solid var(--bdr)}
#oct td.c{text-align:center}
#oct tr.atm td{background:rgba(255,214,0,0.06)}
.oib{display:inline-block;height:6px;border-radius:1px;vertical-align:middle;margin-left:2px}

/* RISK */
.ri{display:flex;align-items:center;gap:10px;padding:4px 12px}
.ri label{font-size:9px;color:var(--muted);width:90px;flex-shrink:0}
.ri input{flex:1;background:var(--bg3);border:1px solid var(--bdr2);color:var(--white);padding:8px 10px;font-family:var(--mono);font-size:12px;border-radius:3px;outline:none;min-width:0}
.ri input:focus{border-color:var(--orange)}
.rres{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--bg3);border-top:1px solid var(--bdr)}
.rres-l{font-size:9px;color:var(--muted)}
.rres-v{font-size:18px;font-weight:900;font-family:var(--cond)}

/* CHART */
.chart-wrap{background:var(--bg2);border:1px solid var(--bdr);border-radius:5px;overflow:hidden;height:360px;display:flex;flex-direction:column}
.chart-hd{display:flex;align-items:center;justify-content:space-between;padding:7px 10px;border-bottom:1px solid var(--bdr);background:var(--bg3);flex-shrink:0}
.chart-hd-t{font-size:8px;font-weight:700;letter-spacing:0.16em;color:var(--muted);text-transform:uppercase}
.chart-body{flex:1}
.tf-row{display:flex;gap:4px}
.tf{padding:4px 10px;border-radius:2px;font-family:var(--mono);font-size:9px;font-weight:700;cursor:pointer;border:1px solid var(--bdr);background:transparent;color:var(--muted)}
.tf.on{background:var(--orange);color:#000;border-color:var(--orange)}

/* AI */
#ai-msgs{height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:10px 12px;background:var(--bg2);border:1px solid var(--bdr);border-radius:5px}
.msg{padding:9px 12px;border-radius:5px;font-size:10px;line-height:1.6}
.msg.bot{background:var(--bg3);border:1px solid var(--bdr);border-left:3px solid var(--teal)}
.msg.user{background:rgba(255,109,0,0.08);border:1px solid rgba(255,109,0,0.2);border-left:3px solid var(--orange)}
.typing{padding:9px 12px;display:flex;gap:4px;align-items:center;background:var(--bg3);border:1px solid var(--bdr);border-left:3px solid var(--teal);border-radius:5px}
.typing span{width:5px;height:5px;border-radius:50%;background:var(--teal);animation:bo 0.9s ease-in-out infinite}
.typing span:nth-child(2){animation-delay:0.15s}.typing span:nth-child(3){animation-delay:0.3s}
@keyframes bo{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.ai-bar{display:flex;gap:6px;margin-top:6px}
#ai-in{flex:1;background:var(--bg2);border:1px solid var(--bdr2);color:var(--white);padding:10px 12px;font-family:var(--mono);font-size:10px;border-radius:4px;outline:none}
#ai-in:focus{border-color:var(--teal)}
#ai-btn{padding:0 14px;background:var(--teal);color:#000;border:none;font-family:var(--cond);font-size:13px;font-weight:800;cursor:pointer;border-radius:4px}
#ai-btn:disabled{opacity:0.4}

/* NEWS */
.ni{padding:8px 12px;border-bottom:1px solid var(--bdr)}
.ni-top{display:flex;align-items:center;gap:5px;margin-bottom:3px}
.ni-sev{font-size:7px;font-weight:800;padding:2px 6px;border-radius:2px;letter-spacing:0.1em}
.ni-src{font-size:8px;color:var(--muted)}
.ni-body{font-size:9px;line-height:1.4}

/* LOG */
#logbar{height:24px;background:var(--bg3);border-top:1px solid var(--bdr);display:flex;align-items:center;padding:0 12px;font-size:8px;flex-shrink:0;gap:8px}
#logtxt{color:var(--muted);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* TOAST */
#toast{position:fixed;top:60px;left:50%;transform:translateX(-50%) translateY(-8px);background:var(--bg3);border:1px solid var(--orange);border-radius:5px;padding:10px 18px;font-family:var(--cond);font-size:15px;font-weight:800;color:var(--orange);opacity:0;pointer-events:none;transition:all 0.3s;z-index:999;box-shadow:0 0 24px rgba(255,109,0,0.3)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.up{color:var(--green)}.dn{color:var(--red)}.neu{color:var(--muted)}
</style>
</head>
<body>
<div id="app">

<!-- SETUP SCREEN — shown until URL is set -->
<div id="setup" class="hide">
  <div class="setup-box">
    <div class="setup-title">⚙ CONNECT SERVER</div>
    <div class="setup-sub">
      Paste your Railway URL below.<br>
      This is shown after you deploy bn_server.py to Railway.<br>
      It looks like: <span style="color:var(--teal)">https://your-app.up.railway.app</span>
    </div>
    <div class="setup-label">Railway Server URL</div>
    <input id="server-url" type="url" placeholder="https://your-app.up.railway.app" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"/>
    <button class="setup-btn" onclick="saveAndConnect()">CONNECT & SAVE</button>
    <div id="test-result"></div>
    <div class="setup-hint">
      Your URL is saved in this browser — you only enter it once.<br>
      Tap the green LIVE pill anytime to change it.
    </div>
  </div>
</div>

<!-- TOP BAR -->
<div id="topbar">
  <div class="logo">
    <div class="dot"></div>
    <div><div class="logo-name">BN TERMINAL</div><div class="logo-sub">Upstox · Live</div></div>
  </div>
  <div class="top-right">
    <div id="conn-pill" onclick="showSetup()"><div class="bd" style="background:var(--muted)"></div><span id="conn-txt">CONNECTING</span></div>
    <div id="clock">—:—:—</div>
  </div>
</div>

<!-- PRICE STRIP -->
<div id="price-strip">
  <div>
    <div id="spot-big" style="font-family:var(--cond);font-size:32px;font-weight:900;color:var(--muted)">—</div>
    <div id="chg" style="font-size:11px;font-weight:700;color:var(--muted)">Auto-live prices</div>
  </div>
  <div class="ohlc">
    <div class="oi"><div class="oi-l">OPEN</div><div id="d-o" class="oi-v">—</div></div>
    <div class="oi"><div class="oi-l">HIGH</div><div id="d-h" class="oi-v up">—</div></div>
    <div class="oi"><div class="oi-l">LOW</div> <div id="d-l" class="oi-v dn">—</div></div>
    <div class="oi"><div class="oi-l">VWAP</div><div id="d-vw" class="oi-v">—</div></div>
  </div>
  <div id="upd-info" style="margin-left:auto;font-size:8px;color:var(--muted);text-align:right;flex-shrink:0">
    AUTO-LIVE<br><span id="upd-ts" style="color:var(--dim)">—</span>
  </div>
</div>

<!-- GLOBALS -->
<div id="globals">
  <div class="g"><div class="g-l">VIX</div>  <div id="g-vix" class="g-v">—</div></div>
  <div class="g"><div class="g-l">PCR</div>   <div id="g-pcr" class="g-v">—</div></div>
  <div class="g"><div class="g-l">S&amp;P</div><div id="g-sp"  class="g-v">—</div></div>
  <div class="g"><div class="g-l">CRUDE</div> <div id="g-cr"  class="g-v">—</div></div>
  <div class="g"><div class="g-l">GOLD</div>  <div id="g-gd"  class="g-v">—</div></div>
  <div class="g"><div class="g-l">₹/USD</div><div id="g-usd" class="g-v">—</div></div>
</div>

<!-- TABS -->
<div id="tabs">
  <div class="tab on" onclick="goTab(0)">📊 SIGNAL</div>
  <div class="tab"    onclick="goTab(1)">📈 CHART</div>
  <div class="tab"    onclick="goTab(2)">⛓ LEVELS</div>
  <div class="tab"    onclick="goTab(3)">🤖 ARIA</div>
  <div class="tab"    onclick="goTab(4)">📋 TRADES</div>
</div>

<!-- PAGES -->
<div id="pages">

  <!-- PAGE 0: SIGNAL -->
  <div class="page on" id="page-0">
    <div class="card">
      <div class="card-hd">SIGNAL ENGINE<span id="sig-ts" style="font-weight:400">—</span></div>
      <div id="sig-body">
        <div class="sw"><div style="font-size:34px;margin-bottom:8px">⏳</div><div class="sw-t">CONNECTING</div><div class="sw-m">Connecting to cloud server...<br>Prices and signals load automatically.</div></div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd">Day Bias</div>
      <div style="padding:10px 12px">
        <div id="tl" class="tlabel" style="color:var(--muted)">Loading...</div>
        <div class="tbar"><div id="tf2" class="tbf" style="width:50%;background:var(--muted)"></div></div>
        <div id="t-fac" style="font-size:8px;color:var(--muted)">—</div>
        <div id="t-str" class="tstrat" style="background:rgba(74,96,112,0.2);color:var(--muted)">—</div>
      </div>
    </div>

    <div>
      <div class="sec-title">Live Indicators</div>
      <div class="ig">
        <div class="ind"><div class="ind-l">EMA 9</div><div id="i-e9" class="ind-v" style="color:var(--muted)">—</div><div class="ind-b"><div id="i-e9b" class="ind-bf" style="width:50%;background:var(--green)"></div></div></div>
        <div class="ind"><div class="ind-l">EMA 21</div><div id="i-e21" class="ind-v" style="color:var(--muted)">—</div></div>
        <div class="ind"><div class="ind-l">RSI 14</div><div id="i-rsi" class="ind-v" style="color:var(--muted)">—</div><div class="ind-b"><div id="i-rsib" class="ind-bf" style="width:50%;background:var(--yellow)"></div></div></div>
        <div class="ind"><div class="ind-l">VWAP</div><div id="i-vw" class="ind-v" style="color:var(--muted)">—</div></div>
        <div class="ind"><div class="ind-l">S.TREND</div><div id="i-st" class="ind-v" style="color:var(--muted)">—</div></div>
        <div class="ind"><div class="ind-l">PCR</div><div id="i-pcr" class="ind-v" style="color:var(--muted)">—</div></div>
      </div>
    </div>

    <div class="mg">
      <div class="mi"><div class="mi-l">PCR (OI)</div><div id="m-pcr" class="mi-v" style="color:var(--muted)">—</div><div id="m-bias" class="mi-s">—</div></div>
      <div class="mi"><div class="mi-l">Max Pain</div><div id="m-mp" class="mi-v" style="color:var(--yellow)">—</div><div class="mi-s">₹ strike</div></div>
      <div class="mi"><div class="mi-l">VIX</div><div id="m-vix" class="mi-v" style="color:var(--muted)">—</div><div id="m-vixs" class="mi-s">—</div></div>
      <div class="mi"><div class="mi-l">Total OI</div><div id="m-oi" class="mi-v" style="color:var(--muted)">—</div><div class="mi-s">CE+PE Lakh</div></div>
    </div>

    <div class="card">
      <div class="card-hd">Risk Calculator</div>
      <div style="padding:6px 0">
        <div class="ri"><label>Capital ₹</label><input id="rc-c" type="number" value="50000" inputmode="numeric"/></div>
        <div class="ri"><label>Entry ₹</label><input id="rc-e" type="number" placeholder="option premium" inputmode="decimal"/></div>
        <div class="ri"><label>Stop Loss ₹</label><input id="rc-s" type="number" placeholder="sl premium" inputmode="decimal"/></div>
      </div>
      <div class="rres"><div><div class="rres-l">LOTS (1% risk · 15 qty)</div></div><div id="rc-lots" class="rres-v" style="color:var(--teal)">—</div></div>
      <div class="rres" style="border-top:1px solid var(--bdr)"><div class="rres-l">Max Risk ₹</div><div id="rc-mr" class="rres-v" style="color:var(--red)">—</div></div>
    </div>
  </div>

  <!-- PAGE 1: CHART -->
  <div class="page" id="page-1">
    <div class="chart-wrap">
      <div class="chart-hd">
        <span class="chart-hd-t">BANKNIFTY · UPSTOX LIVE</span>
        <div class="tf-row">
          <button class="tf on" onclick="loadChart(1)">1m</button>
        </div>
      </div>
      <div class="chart-body" style="position:relative">
        <div id="lw_chart" style="width:100%;height:320px"></div>
        <div id="chart-loading" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-family:var(--cond);font-size:13px;color:var(--muted);font-weight:700;letter-spacing:0.1em;text-align:center">LOADING...</div>
        <div id="chart-legend" style="position:absolute;top:8px;left:8px;font-size:9px;color:var(--white);background:rgba(6,10,16,0.85);padding:4px 8px;border-radius:3px;pointer-events:none;z-index:10;letter-spacing:0.05em"></div>
      </div>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;padding:4px 2px">
      <span style="font-size:8px;color:var(--muted);display:flex;align-items:center;gap:4px"><span style="width:14px;height:2px;background:#00BFA5;display:inline-block;border-radius:1px"></span>EMA 9</span>
      <span style="font-size:8px;color:var(--muted);display:flex;align-items:center;gap:4px"><span style="width:14px;height:2px;background:#FF6D00;display:inline-block;border-radius:1px"></span>EMA 21</span>
      <span style="font-size:8px;color:var(--muted);display:flex;align-items:center;gap:4px"><span style="width:14px;height:2px;background:#FFD600;display:inline-block;border-radius:1px;opacity:0.7"></span>VWAP</span>
    </div>
  </div>

  <!-- PAGE 2: LEVELS -->
  <div class="page" id="page-2">
    <div>
      <div class="sec-title">Support &amp; Resistance · Daily Pivot</div>
      <div class="sr-list" id="sr-list"><div style="color:var(--muted);text-align:center;padding:20px;font-size:10px">Loading...</div></div>
    </div>
    <div class="card">
      <div class="card-hd">Option Chain (Synthetic)<span id="oc-b" style="font-size:7px;font-weight:700;padding:2px 6px;border-radius:2px;background:rgba(255,214,0,0.1);color:var(--yellow)">WEEKLY</span></div>
      <div class="oc-scroll">
        <table id="oct">
          <thead><tr>
            <th style="text-align:left;color:var(--green)">CE LTP</th><th style="color:var(--teal)">CE OI</th>
            <th class="c">STRIKE</th>
            <th style="color:var(--orange)">PE OI</th><th style="text-align:right;color:var(--red)">PE LTP</th>
          </tr></thead>
          <tbody id="oc-body"><tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- PAGE 3: ARIA -->
  <div class="page" id="page-3">
    <div id="ai-msgs">
      <div class="msg bot">👋 I'm <strong>ARIA</strong>.<br><br>Your server is running in the cloud 24/7 — prices update automatically every 30 seconds.<br><br>I'll auto-explain every signal. Ask me anything.<br><br><span style="color:var(--teal)">"Why BUY?" · "What is PCR?" · "Is this a good setup?"</span></div>
    </div>
    <div class="ai-bar">
      <input id="ai-in" placeholder="Ask ARIA..." onkeydown="if(event.key==='Enter'){event.preventDefault();sendAI()}"/>
      <button id="ai-btn" onclick="sendAI()">ASK</button>
    </div>
    <div class="card" style="margin-top:0">
      <div class="card-hd">Market Alerts</div>
      <div id="news-list"></div>
    </div>
  </div>


  <!-- PAGE 4: TRADES -->
  <div class="page" id="page-4">
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px">
      <div class="mi"><div class="mi-l">Capital</div><div class="mi-v" style="color:var(--white);font-size:16px">&#8377;1,00,000</div></div>
      <div class="mi"><div class="mi-l">Available</div><div id="pt-avail" class="mi-v" style="color:var(--teal);font-size:16px">-</div></div>
      <div class="mi"><div class="mi-l">Total P&L</div><div id="pt-pnl" class="mi-v" style="font-size:16px">-</div></div>
      <div class="mi"><div class="mi-l">Win Rate</div><div id="pt-wr" class="mi-v" style="color:var(--green);font-size:16px">-</div></div>
    </div>
    <div class="card">
      <div class="card-hd">OPEN POSITION<span id="pt-open-time" style="font-weight:400;color:var(--muted)"></span></div>
      <div id="pt-open-body" style="padding:14px">
        <div style="text-align:center;color:var(--muted);font-size:10px;padding:10px">No open position</div>
      </div>
    </div>
    <div style="display:flex;gap:8px">
      <button onclick="manualClose()" style="flex:1;padding:10px;background:var(--red);color:#fff;border:none;font-family:var(--cond);font-size:14px;font-weight:800;cursor:pointer;border-radius:4px">CLOSE TRADE</button>
      <button onclick="resetAccount()" style="padding:10px 16px;background:var(--dim);color:var(--muted);border:1px solid var(--bdr);font-family:var(--cond);font-size:12px;font-weight:800;cursor:pointer;border-radius:4px">RESET</button>
    </div>
    <div class="card">
      <div class="card-hd">TRADE HISTORY<span id="pt-count" style="font-weight:400;color:var(--muted)">0 trades</span></div>
      <div id="pt-history" style="max-height:300px;overflow-y:auto">
        <div style="text-align:center;color:var(--muted);font-size:10px;padding:20px">No trades yet</div>
      </div>
    </div>
    <div id="pt-daily" style="border:1px solid rgba(0,191,165,0.2);border-radius:5px;padding:8px 14px;font-size:9px;line-height:1.8;font-weight:700;letter-spacing:0.05em"></div>
    <div style="background:rgba(0,191,165,0.06);border:1px solid rgba(0,191,165,0.2);border-radius:5px;padding:10px 14px;font-size:9px;color:var(--teal);line-height:1.8">
      Signal-based trading | Stop if -2% daily loss | Trade 9:20-15:00 IST | 10min gap | No fixed trade limit
    </div>

    <!-- Phase 2 & 3: Learning Panel -->
    <div class="card">
      <div class="card-hd">🧠 AI LEARNING LOG<span id="learn-days" style="font-weight:400;color:var(--muted)">0 days</span></div>
      <div id="learn-body" style="padding:10px 12px">
        <div style="text-align:center;color:var(--muted);font-size:10px;padding:10px">Learning data builds up after market close each day</div>
      </div>
    </div>
  </div>
</div>
</div>

<div id="logbar"><span id="logtxt">Connecting to cloud server...</span></div>
<div id="toast"><span id="toast-msg"></span></div>

<script>
// ═══════════════════ BN CLOUD TERMINAL ═══════════════════
const STORAGE_KEY = 'bn_server_url';
let SERVER = localStorage.getItem(STORAGE_KEY) || '';
const S = {spot:0,open:0,high:0,low:0,vwap:0,change:0,pct:0,vix:0,sp500chg:0,crudechg:0,goldchg:0,usdinr:0,pcr:0.9,maxPain:0,totCE:0,totPE:0,candles:[],signal:null};
let ariaKey='',ariaHist=[],prevSig=null;

// Clock
setInterval(()=>{const d=new Date(Date.now()+5.5*3600000);document.getElementById('clock').textContent=`${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}:${String(d.getUTCSeconds()).padStart(2,'0')} IST`;},1000);
function log(m,ok=null){document.getElementById('logtxt').textContent=m;document.getElementById('logtxt').style.color=ok===true?'var(--green)':ok===false?'var(--red)':'var(--muted)';}
function goTab(i){document.querySelectorAll('.tab').forEach((t,j)=>t.classList.toggle('on',i===j));document.querySelectorAll('.page').forEach((p,j)=>p.classList.toggle('on',i===j));if(i===1){setTimeout(function(){if(!lwC){initChart();}else{loadChart(1);}},100);}}

// Setup
function showSetup(){document.getElementById('setup').classList.remove('hide');if(SERVER)document.getElementById('server-url').value=SERVER;}
function fetchT(url,ms){return Promise.race([fetch(url,{cache:'no-store'}),new Promise((_,r)=>setTimeout(()=>r(new Error('Timeout')),ms))]);}
async function saveAndConnect(){
  const url=document.getElementById('server-url').value.trim().replace(/\\/+$/,'');
  if(!url.startsWith('http')){showResult('Enter a valid URL starting with https://','var(--red)');return;}
  showResult('Testing connection...','var(--yellow)');
  try{
    const res=await fetchT('/api/status',6000);
    if(!res.ok)throw new Error('Server responded with '+res.status);
    const d=await res.json();
    showResult('✓ Connected! BN ₹'+(d.spot||'loading...'),'var(--green)');
    SERVER=url;
    localStorage.setItem(STORAGE_KEY,SERVER);
    setTimeout(()=>{document.getElementById('setup').classList.add('hide');fetchFromServer();},1200);
  }catch(e){showResult('❌ Cannot connect: '+e.message,'var(--red)');}
}
function showResult(m,c){const el=document.getElementById('test-result');el.style.display='block';el.style.background=c+'22';el.style.color=c;el.style.border='1px solid '+c+'44';el.style.borderRadius='3px';el.style.padding='8px 10px';el.style.fontSize='9px';el.style.lineHeight='1.6';el.textContent=m;}
function setConn(ok,marketOpen,lastSessionTime){
  const p=document.getElementById('conn-pill');const d=p.querySelector('.bd');
  let label,c;
  if(ok&&marketOpen){label='LIVE';c='var(--green)';}
  else if(ok&&!marketOpen){label='CLOSED';c='var(--yellow)';}
  else{label='OFFLINE';c='var(--red)';}
  document.getElementById('conn-txt').textContent=label;
  p.style.color=c;p.style.borderColor=c;d.style.background=c;
  // Show/hide last session banner
  let banner=document.getElementById('session-banner');
  if(!banner){banner=document.createElement('div');banner.id='session-banner';
    banner.style.cssText='background:rgba(255,214,0,0.08);border-bottom:1px solid rgba(255,214,0,0.2);padding:4px 14px;font-size:8px;color:var(--yellow);text-align:center;flex-shrink:0;letter-spacing:0.08em;font-weight:700;';
    const tabs=document.getElementById('tabs');tabs.parentNode.insertBefore(banner,tabs);}
  if(!marketOpen&&lastSessionTime){banner.textContent='MARKET CLOSED · Showing last session: '+lastSessionTime;banner.style.display='block';}
  else{banner.style.display='none';}
}

// Fetch
async function fetchFromServer(){
  if(!SERVER){showSetup();return;}
  try{
    const res=await fetchT('/api/price',8000);
    if(!res.ok)throw new Error('HTTP '+res.status);
    const d=await res.json();
    if(!d.authenticated){
      setConn(false,false,'');
      showLoginAlert();
      return;
    }
    if(!d.spot||d.spot<30000)throw new Error('No valid price');
    const prev=S.spot;
    S.spot=d.spot;S.change=d.change;S.pct=d.pct;
    S.high=d.high;S.low=d.low;S.open=d.open;
    S.vwap=(d.high+d.low+d.spot)/3;
    S.vix=d.vix;S.sp500chg=d.sp500_chg;S.crudechg=d.crude_chg;
    S.goldchg=d.gold_chg;S.usdinr=d.usdinr;
    const now=new Date(Date.now()+5.5*3600000);
    const t=`${String(now.getUTCHours()).padStart(2,'0')}:${String(now.getUTCMinutes()).padStart(2,'0')}`;
    const last=S.candles[S.candles.length-1];
    if(last&&last.t===t){last.c=d.spot;last.h=Math.max(last.h,d.high||d.spot);last.l=Math.min(last.l,d.low||d.spot);}
    else{S.candles.push({t,o:prev||d.spot,h:d.high||d.spot,l:d.low||d.spot,c:d.spot,v:1});}
    if(S.candles.length>200)S.candles=S.candles.slice(-200);
    document.getElementById('upd-ts').textContent=d.last_updated||'—';
    setConn(true,d.market_open!==false,d.last_session_time||'');
    log((d.using_last_session?'Last session \u00b7 ':'Live \u00b7 ')+'BN \u20b9'+d.spot.toLocaleString('en-IN')+' \u00b7 VIX '+d.vix+' \u00b7 '+(d.last_session_time||d.last_updated),d.using_last_session?null:true);
    hideLoginAlert();
    renderAll();
    updateChartTick(d.spot,d.high,d.low);
  }catch(e){
    setConn(false,false,'');
    log('Server error: '+e.message,false);
    showLoginAlert();
  }
}

// All indicator / render functions
function ema(arr,p){const k=2/(p+1);let e=arr[0];for(let i=1;i<arr.length;i++)e=arr[i]*k+e*(1-k);return e;}
function rsi(arr,p=14){if(arr.length<p+1)return 50;let g=0,l=0;for(let i=arr.length-p;i<arr.length;i++){const d=arr[i]-arr[i-1];if(d>0)g+=d;else l-=d;}if(l===0)return 100;return+(100-100/(1+(g/p)/(l/p))).toFixed(1);}
function vwapCalc(cs){return cs.reduce((a,c)=>a+(c.h+c.l+c.c)/3,0)/cs.length;}
function supertrend(cs,aP=10,f=3){if(cs.length<aP+1)return{bull:S.change>=0};const atrs=[];for(let i=1;i<cs.length;i++)atrs.push(Math.max(cs[i].h-cs[i].l,Math.abs(cs[i].h-cs[i-1].c),Math.abs(cs[i].l-cs[i-1].c)));const atr=atrs.slice(-aP).reduce((a,b)=>a+b,0)/aP;const last=cs[cs.length-1];const mid=(last.h+last.l)/2;return{bull:last.c>mid-f*atr};}
function calcPivots(cs){const H=cs.length?Math.max(...cs.map(c=>c.h)):S.high||S.spot;const L=cs.length?Math.min(...cs.map(c=>c.l)):S.low||S.spot;const C=cs.length?cs[cs.length-1].c:S.spot;const p=(H+L+C)/3;return{P:+p.toFixed(0),R1:+(2*p-L).toFixed(0),R2:+(p+H-L).toFixed(0),R3:+(H+2*(p-L)).toFixed(0),S1:+(2*p-H).toFixed(0),S2:+(p-(H-L)).toFixed(0),S3:+(L-2*(H-p)).toFixed(0)};}
function buildOC(spot){const atm=Math.round(spot/100)*100;const strikes=[];for(let i=-6;i<=6;i++)strikes.push(atm+i*100);const ce={},pe={};let totCE=0,totPE=0;const vol=(S.vix||14)/100*Math.sqrt(7/365);for(const s of strikes){const absd=Math.abs(s-spot),isATM=absd<50;const tP=spot*vol*Math.exp(-absd/(spot*0.015+1))*100;ce[s]={ltp:+(Math.max(0.5,Math.max(0,spot-s)+tP).toFixed(1)),oi:Math.round((isATM?500:Math.max(15,500-absd*0.8))*1000),oiChg:+(Math.random()*6-1.5).toFixed(1)};pe[s]={ltp:+(Math.max(0.5,Math.max(0,s-spot)+tP).toFixed(1)),oi:Math.round((isATM?480:Math.max(15,480-absd*0.75))*1000),oiChg:+(Math.random()*6-1.5).toFixed(1)};totCE+=ce[s].oi;totPE+=pe[s].oi;}let maxPain=atm,minLoss=Infinity;for(const s of strikes){let loss=0;for(const ss of strikes){loss+=Math.max(0,ss-s)*(ce[ss]?.oi||0)+Math.max(0,s-ss)*(pe[ss]?.oi||0);}if(loss<minLoss){minLoss=loss;maxPain=s;}}const pcr=+(totPE/totCE).toFixed(2);S.pcr=pcr;S.maxPain=maxPain;S.totCE=totCE;S.totPE=totPE;return{strikes,ce,pe,pcr,maxPain,totCE,totPE};}
// ═══════════════════ UPGRADED SIGNAL ENGINE v2 ═══════════════════
// Captures big moves, adapts to VIX, gap analysis, momentum filter

// ═══════════════════ SIGNAL ENGINE v3 ═══════════════════
// Uses market behaviour + confirmation + dynamic management

function detectMarketRegime(cs, spot, vix) {
  if (cs.length < 10) return {regime:'UNKNOWN', bias:0, gapPct:0, f15Dir:0, momentumPct:0, atr:50, dayOpen:spot};
  const cl = cs.map(c=>c.c);
  const dayOpen = cs[0].o;
  const gapPct = ((spot - dayOpen) / dayOpen) * 100;
  const first15 = cs.slice(0, Math.min(15, cs.length));
  const f15Dir = first15[first15.length-1].c - first15[0].o;
  const momentum = cl[cl.length-1] - cl[Math.max(0, cl.length-20)];
  const momentumPct = (momentum / cl[Math.max(0, cl.length-20)]) * 100;
  const atrs = [];
  for(let i=1;i<cs.length;i++) atrs.push(Math.max(cs[i].h-cs[i].l,Math.abs(cs[i].h-cs[i-1].c),Math.abs(cs[i].l-cs[i-1].c)));
  const atr = atrs.length ? atrs.reduce((a,b)=>a+b,0)/atrs.length : 50;
  let regime, bias;
  if(Math.abs(momentumPct)>0.8){regime=momentumPct>0?'TRENDING_UP':'TRENDING_DOWN';bias=momentumPct>0?3:-3;}
  else if(atr<30){regime='RANGING';bias=0;}
  else{regime='NORMAL';bias=momentumPct>0?1:-1;}
  return {regime,bias,gapPct,f15Dir,momentum,momentumPct,atr,dayOpen};
}

function computeTrend(cs, spot) {
  if (cs.length < 3) return null;
  const cl = cs.map(c=>c.c);
  const e9=ema(cl,9),e21=ema(cl,21),rs=rsi(cl,14),vw=vwapCalc(cs),st=supertrend(cs);
  const slope=(cl[cl.length-1]-cl[Math.max(0,cl.length-6)])/6;
  const bf=[['Above VWAP',spot>vw],['EMA9>EMA21',e9>e21],['Supertrend Bull',st.bull],['RSI>52',rs>52],['Trending Up',slope>5],['S&P+',S.sp500chg>0],['VIX<18',S.vix<18||S.vix===0],['Above Open',S.open>0&&spot>S.open]];
  const brf=[['Below VWAP',spot<vw],['EMA9<EMA21',e9<e21],['Supertrend Bear',!st.bull],['RSI<48',rs<48],['Trending Down',slope<-5],['S&P-',S.sp500chg<0],['VIX>18',S.vix>18],['Below Open',S.open>0&&spot<S.open]];
  const bs=bf.filter(f=>f[1]).length,br=brf.filter(f=>f[1]).length,net=bs-br;
  let label,col,strat;
  if(net>=5){label='STRONGLY BULLISH';col='var(--green)';strat='Strong uptrend — buy dips to VWAP';}
  else if(net>=3){label='BULLISH';col='#66BB6A';strat='Bullish — buy pullbacks';}
  else if(net>=1){label='MILDLY BULLISH';col='var(--teal)';strat='Cautious CE only';}
  else if(net>=-1){label='SIDEWAYS';col='var(--yellow)';strat='Wait for breakout';}
  else if(net>=-3){label='MILDLY BEARISH';col='#FFA040';strat='Cautious PE only';}
  else if(net>=-5){label='BEARISH';col='#FF7043';strat='Sell rallies';}
  else{label='STRONGLY BEARISH';col='var(--red)';strat='Strong downtrend — no long trades';}
  return{label,col,pct:Math.round(bs/8*100),strat,bs,br,factors:bf.filter(f=>f[1]).map(f=>f[0]).join(' · ')||'—',meta:{e9:+e9.toFixed(0),e21:+e21.toFixed(0),rsi:rs,vwap:+vw.toFixed(0),st}};
}

function computeSignal(cs, spot) {
  const piv = calcPivots(cs.length ? cs : []);
  const atm = Math.round(spot/100)*100;
  const base = {signal:'WAIT',conf:0,gate:'',strike:atm,otype:'-',entry:null,sl:null,
    t1:piv.R1,t2:piv.R2,t3:piv.R3,pivots:piv,cl:{},bs:0,br:0,
    meta:{e9:0,e21:0,rsi:50,vwap:S.vwap,st:{bull:S.change>=0}},behaviour:null};

  if(cs.length < 10){base.gate='Building data... ('+cs.length+'/10 candles)';return base;}

  const cl = cs.map(c=>c.c);
  const e9=ema(cl,9),e21=ema(cl,21),rs=rsi(cl,14),vw=vwapCalc(cs),st=supertrend(cs);
  const vix=S.vix||0;
  const regime=detectMarketRegime(cs,spot,vix);
  const behaviour=readMarketBehaviour(cs,spot,vix,S.pcr);

  // IST time
  const ist=new Date(Date.now()+5.5*3600000);
  const hh=ist.getUTCHours(),mm=ist.getUTCMinutes();
  const curMins=hh*60+mm;

  // No trade in first 5 min or last 30 min
  if(curMins < 9*60+20){base.gate='Pre-market — waiting for 9:20 AM';return base;}
  if(curMins > 15*60){base.gate='Market closing — no new trades';return base;}

  // Need minimum 10 candles of confirmed data
  const meta={e9:+e9.toFixed(0),e21:+e21.toFixed(0),rsi:rs,vwap:+vw.toFixed(0),st};

  // ── STRONG TREND SIGNALS ──
  if(behaviour.action==='BUY_CALL' && behaviour.strength>=50){
    const conf=Math.min(92, behaviour.strength);
    return{...base,signal:'BUY',conf,gate:null,otype:'CE',
      entry:piv.R1,sl:Math.max(piv.S1, spot-150),t1:piv.R1,t2:piv.R2,t3:piv.R3,
      cl:{'Strong Uptrend':true,'Above VWAP':true,'EMA Bull':e9>e21,'Vol Confirmed':true},
      bs:4,br:0,meta,behaviour:behaviour.behaviour,
      regime_note:behaviour.reason};
  }

  if(behaviour.action==='BUY_PUT' && behaviour.strength>=50){
    const conf=Math.min(92, behaviour.strength);
    return{...base,signal:'SELL',conf,gate:null,otype:'PE',
      entry:piv.S1,sl:Math.min(piv.R1, spot+150),t1:piv.S1,t2:piv.S2,t3:piv.S3,
      cl:{'Strong Downtrend':true,'Below VWAP':true,'EMA Bear':e9<e21,'Vol Confirmed':true},
      bs:0,br:4,meta,behaviour:behaviour.behaviour,
      regime_note:behaviour.reason};
  }

  // ── REVERSAL SIGNALS (catching turns) ──
  if(behaviour.action==='WATCH_SHORT' && behaviour.strength>=45){
    if(e9<e21 && spot<vw){
      const conf=Math.min(78, behaviour.strength+10);
      return{...base,signal:'SELL',conf,gate:null,otype:'PE',
        entry:piv.S1,sl:spot+120,t1:piv.S1,t2:piv.S2,t3:piv.S3,
        cl:{'Trending Bear':true,'Below VWAP':true,'EMA Bear':e9<e21},
        bs:0,br:3,meta,behaviour:'TREND_CONTINUATION',regime_note:'Trend continuation entry'};
    }
  }
  if(behaviour.action==='WATCH_SHORT' && behaviour.strength>=50 && rs>65){
    const conf=Math.min(80, behaviour.strength+5);
    return{...base,signal:'SELL',conf,gate:null,otype:'PE',
      entry:piv.S1,sl:spot+100,t1:piv.S1,t2:piv.S2,t3:piv.S3,
      cl:{'RSI Overbought':rs>68,'Losing Momentum':true,'Reversal Setup':true},
      bs:0,br:3,meta,behaviour:'REVERSAL_DOWN',regime_note:behaviour.reason};
  }

  // On trending days fire on WATCH signals too
  if(behaviour.action==='WATCH_LONG' && behaviour.strength>=45){
    // Only if clear bull trend
    if(e9>e21 && spot>vw){
      const conf=Math.min(78, behaviour.strength+10);
      return{...base,signal:'BUY',conf,gate:null,otype:'CE',
        entry:piv.R1,sl:spot-120,t1:piv.R1,t2:piv.R2,t3:piv.R3,
        cl:{'Trending Bull':true,'Above VWAP':true,'EMA Bull':e9>e21},
        bs:3,br:0,meta,behaviour:'TREND_CONTINUATION',regime_note:'Trend continuation entry'};
    }
  }
  if(behaviour.action==='WATCH_LONG' && behaviour.strength>=50 && rs<35){
    const conf=Math.min(80, behaviour.strength+5);
    return{...base,signal:'BUY',conf,gate:null,otype:'CE',
      entry:piv.R1,sl:spot-100,t1:piv.R1,t2:piv.R2,t3:piv.R3,
      cl:{'RSI Oversold':rs<32,'Gaining Momentum':true,'Reversal Setup':true},
      bs:3,br:0,meta,behaviour:'REVERSAL_UP',regime_note:behaviour.reason};
  }

  // ── STANDARD SIGNALS (normal market) ──
  const bull={'Price>VWAP':spot>vw,'EMA9>EMA21':e9>e21,'Supertrend Bull':st.bull,
    'RSI 45-65':rs>=45&&rs<=65,'Momentum Up':regime.momentumPct>0.1,
    'PCR>0.9':S.pcr>0.9,'RSI>50':rs>50,'S&P+':S.sp500chg>0};
  const bear={'Price<VWAP':spot<vw,'EMA9<EMA21':e9<e21,'Supertrend Bear':!st.bull,
    'RSI 35-55':rs>=35&&rs<=55,'Momentum Down':regime.momentumPct<-0.1,
    'PCR<1.0':S.pcr<1.0,'RSI<50':rs<50,'S&P-':S.sp500chg<0};
  const bs=Object.values(bull).filter(Boolean).length;
  const br=Object.values(bear).filter(Boolean).length;
  const threshold = vix>20 ? 5 : 4;
  let raw='WAIT',conds=bs>=br?bull:bear;
  if(bs>=threshold){raw='BUY';conds=bull;}
  else if(br>=threshold){raw='SELL';conds=bear;}
  const score=raw==='BUY'?bs:raw==='SELL'?br:Math.max(bs,br);
  const conf=raw==='WAIT'?Math.round(score/8*100):Math.min(88,58+score*5);
  if(conf<60&&raw!=='WAIT'){base.gate='Confidence '+conf+'% below threshold';return base;}
  return{signal:raw,raw,conf,score,gate:null,strike:atm,
    otype:raw==='BUY'?'CE':raw==='SELL'?'PE':'-',
    entry:raw==='BUY'?piv.R1:raw==='SELL'?piv.S1:null,
    sl:raw==='BUY'?Math.max(piv.S1,spot-150):raw==='SELL'?Math.min(piv.R1,spot+150):null,
    t1:raw==='BUY'?piv.R1:piv.S1,t2:raw==='BUY'?piv.R2:piv.S2,
    t3:raw==='BUY'?piv.R3:piv.S3,
    pivots:piv,cl:conds,bs,br,meta,behaviour:behaviour.behaviour,
    regime_note:behaviour.reason};
}


function renderAll(){if(!S.spot)return;const f=n=>Math.round(n).toLocaleString('en-IN');const cc=n=>n>=0?'up':'dn';const fp=n=>(n>=0?'+':'')+n.toFixed(2)+'%';const el=document.getElementById('spot-big');el.textContent='₹'+f(S.spot);el.className=cc(S.change);el.style.fontFamily='var(--cond)';el.style.fontSize='32px';el.style.fontWeight='900';document.getElementById('chg').textContent=(S.change>=0?'▲':'▼')+Math.abs(S.change).toFixed(2)+' ('+Math.abs(S.pct).toFixed(2)+'%)';document.getElementById('chg').className=cc(S.change);document.getElementById('d-o').textContent=f(S.open);document.getElementById('d-h').textContent=f(S.high);document.getElementById('d-l').textContent=f(S.low);document.getElementById('d-vw').textContent=f(S.vwap);document.getElementById('g-vix').textContent=S.vix?S.vix.toFixed(1):'—';document.getElementById('g-vix').className='g-v '+(S.vix>20?'dn':S.vix>15?'neu':'up');document.getElementById('g-pcr').textContent=S.pcr.toFixed(2);document.getElementById('g-pcr').className='g-v '+(S.pcr>=1.2?'up':S.pcr>=0.9?'neu':'dn');['g-sp','g-cr','g-gd'].forEach((id,i)=>{const v=[S.sp500chg,S.crudechg,S.goldchg][i];document.getElementById(id).textContent=v?fp(v):'—';document.getElementById(id).className='g-v '+cc(v);});document.getElementById('g-usd').textContent=S.usdinr?S.usdinr.toFixed(1):'—';const oc=buildOC(S.spot);renderOC(oc);renderMinis(oc);const t=computeTrend(S.candles,S.spot);renderTrend(t);const sig=computeSignal(S.candles,S.spot);S.signal=sig;renderSignal(sig);checkPaperTrade(sig);
  // TradeBrain tick - monitor open trade every price update
  if(TradeBrain.active && S.candles.length>=3){
    const cl2=S.candles.map(c=>c.c);
    const e9t=ema(cl2,9),e21t=ema(cl2,21),vwt=vwapCalc(S.candles),rst=rsi(cl2,14);
    const result=TradeBrain.tick(S.spot,S.candles,e9t,e21t,vwt,rst);
    if(result&&result.action==='EXIT'){
      // Signal server to close trade
      fetch('/api/trades/close_reason',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({reason:result.reason,spot:S.spot})
      }).then(()=>fetchTrades());
      TradeBrain.reset();
      toast('Trade exited: '+result.reason);
    }
    updateBrainDisplay(result);
  }renderInds(sig?.meta||t?.meta);renderSR(sig?.pivots,S.spot);if(prevSig&&prevSig!=='WAIT'&&sig?.signal!==prevSig&&sig?.signal!=='WAIT')toast('Signal: '+prevSig+' → '+sig.signal);if((!prevSig||prevSig==='WAIT')&&sig?.signal!=='WAIT'){ariaExplain(sig);goTab(0);}prevSig=sig?.signal;}

function renderTrend(t){if(!t){document.getElementById('tl').textContent=S.candles.length<3?'Collecting...':'—';return;}document.getElementById('tl').textContent=t.label;document.getElementById('tl').style.color=t.col;document.getElementById('tf2').style.width=t.pct+'%';document.getElementById('tf2').style.background=t.col;document.getElementById('t-fac').textContent='Bull '+t.bs+'/8 · Bear '+t.br+'/8 · '+t.factors;document.getElementById('t-str').textContent=t.strat;document.getElementById('t-str').style.background=t.col+'22';document.getElementById('t-str').style.color=t.col;}
function renderSignal(sig){const body=document.getElementById('sig-body');const ist=new Date(Date.now()+5.5*3600000);document.getElementById('sig-ts').textContent=`${String(ist.getUTCHours()).padStart(2,'0')}:${String(ist.getUTCMinutes()).padStart(2,'0')} IST`;if(!sig||sig.signal==='WAIT'){const m=sig?.gate||(sig?`Bull ${sig.bs}/8 · Bear ${sig.br}/8`:'Waiting...');body.innerHTML=`<div class="sw"><div style="font-size:32px;margin-bottom:6px">⏸</div><div class="sw-t">WAIT</div><div class="sw-m">${m}${sig&&sig.bs+sig.br>0?`<br><br><span style="color:var(--dim)">Bull: ${sig.bs}/8 · Bear: ${sig.br}/8 · Conf: ${sig.conf}%</span>`:''}</div></div>`;return;}const isBuy=sig.signal==='BUY';const col=isBuy?'var(--green)':'var(--red)';const bg=isBuy?'rgba(0,230,118,0.04)':'rgba(255,23,68,0.04)';const f=n=>n?Math.round(n).toLocaleString('en-IN'):'—';const conds=Object.entries(sig.cl);const met=conds.filter(([,v])=>v).length;body.innerHTML=`<div class="sig-ac" style="background:${bg}"><div class="sv" style="color:${col}">${isBuy?'BUY CALL':'BUY PUT'}</div><div class="ss" style="color:${col}">${isBuy?'BULLISH':'BEARISH'} SETUP</div><div class="sb" style="background:${col}18;color:${col};border:1px solid ${col}44">${sig.strike} ${sig.otype} · WEEKLY</div>${sig.behaviour?`<div style="font-size:8px;color:var(--muted);padding:4px 16px;background:var(--bg3)">${sig.behaviour}${sig.regime_note?" — "+sig.regime_note:""}</div>`:""}}</div><div class="lg"><div class="lc" style="border-color:var(--teal)"><div class="lc-l">ENTRY</div><div class="lc-v" style="color:var(--teal)">₹${f(sig.entry)}</div></div><div class="lc" style="border-color:var(--red)"><div class="lc-l">STOP LOSS</div><div class="lc-v" style="color:var(--red)">₹${f(sig.sl)}</div></div><div class="lc" style="border-color:var(--green)"><div class="lc-l">TARGET 1</div><div class="lc-v" style="color:var(--green)">₹${f(sig.t1)}</div></div><div class="lc" style="border-color:var(--yellow)"><div class="lc-l">TARGET 2</div><div class="lc-v" style="color:var(--yellow)">₹${f(sig.t2)}</div></div></div><div class="cr"><span style="font-size:9px;color:var(--muted);flex-shrink:0">CONFIDENCE</span><div class="cb"><div class="cf" style="width:${sig.conf}%;background:${sig.conf>=75?'var(--green)':sig.conf>=60?'var(--yellow)':'var(--red)'}"></div></div><span style="font-size:15px;font-weight:900;font-family:var(--cond);color:${sig.conf>=75?'var(--green)':sig.conf>=60?'var(--yellow)':'var(--red)'};flex-shrink:0">${sig.conf}%</span><span style="font-size:9px;color:var(--muted);flex-shrink:0">${met}/${conds.length}</span></div><div class="cds">${conds.map(([k,v])=>`<span class="cd" style="background:${v?'rgba(0,230,118,0.1)':'rgba(74,96,112,0.15)'};color:${v?'var(--green)':'var(--muted)'}">${v?'✓':'✗'} ${k}</span>`).join('')}</div><div class="sig-foot">⚠ Exit if BN ${isBuy?'closes below':'closes above'} ₹${f(sig.sl)} · T3: ₹${f(sig.t3)}</div>`;}
function renderInds(meta){if(!meta)return;document.getElementById('i-e9').textContent=meta.e9?.toLocaleString('en-IN')||'—';document.getElementById('i-e9').style.color=meta.e9>meta.e21?'var(--green)':'var(--red)';document.getElementById('i-e21').textContent=meta.e21?.toLocaleString('en-IN')||'—';document.getElementById('i-rsi').textContent=meta.rsi||'—';document.getElementById('i-rsi').style.color=meta.rsi>65?'var(--red)':meta.rsi<35?'var(--green)':meta.rsi>50?'var(--green)':'var(--orange)';document.getElementById('i-rsib').style.width=(meta.rsi||50)+'%';document.getElementById('i-rsib').style.background=meta.rsi>70?'var(--red)':meta.rsi<30?'var(--green)':'var(--yellow)';document.getElementById('i-vw').textContent=meta.vwap?.toLocaleString('en-IN')||'—';document.getElementById('i-vw').style.color=S.spot>meta.vwap?'var(--green)':'var(--red)';document.getElementById('i-st').textContent=meta.st?.bull?'BULL ▲':'BEAR ▼';document.getElementById('i-st').style.color=meta.st?.bull?'var(--green)':'var(--red)';document.getElementById('i-pcr').textContent=S.pcr.toFixed(2);document.getElementById('i-pcr').style.color=S.pcr>=1.2?'var(--green)':S.pcr>=0.9?'var(--teal)':'var(--red)';}
function renderMinis(oc){if(!oc)return;const pcr=oc.pcr;const pEl=document.getElementById('m-pcr');pEl.textContent=pcr.toFixed(2);pEl.style.color=pcr>=1.3?'var(--green)':pcr>=1.0?'var(--teal)':pcr>=0.7?'var(--yellow)':'var(--red)';document.getElementById('m-bias').textContent=pcr>=1.2?'Bullish ↑':pcr>=0.9?'Neutral':'Bearish ↓';document.getElementById('m-mp').textContent='₹'+oc.maxPain.toLocaleString('en-IN');document.getElementById('m-oi').textContent=((oc.totCE+oc.totPE)/100000).toFixed(1)+'L';const vix=S.vix;document.getElementById('m-vix').textContent=vix?vix.toFixed(1):'—';document.getElementById('m-vix').style.color=vix<14?'var(--green)':vix<18?'var(--teal)':vix<22?'var(--yellow)':'var(--red)';document.getElementById('m-vixs').textContent=vix?vix<14?'CALM':vix<18?'NORMAL':vix<22?'ELEVATED':'DANGER':'—';}
function renderOC(oc){if(!oc)return;const spot=S.spot,atm=Math.round(spot/100)*100;const maxOI=Math.max(...oc.strikes.map(s=>Math.max(oc.ce[s]?.oi||0,oc.pe[s]?.oi||0)),1);let html='';for(const s of oc.strikes){const c=oc.ce[s]||{ltp:0,oi:0},p=oc.pe[s]||{ltp:0,oi:0};const isATM=Math.abs(s-spot)<50;const cw=Math.round((c.oi/maxOI)*28),pw=Math.round((p.oi/maxOI)*28);const oiK=n=>n>=100000?(n/100000).toFixed(1)+'L':(n/1000).toFixed(0)+'K';html+=`<tr${isATM?' class="atm"':''}><td style="text-align:left;color:var(--green)">₹${c.ltp}<span class="oib" style="width:${cw}px;background:var(--teal)"></span></td><td style="color:var(--teal)">${oiK(c.oi)}</td><td class="c" style="font-weight:900${isATM?';color:var(--yellow)':''}">${s.toLocaleString('en-IN')}${isATM?'<br><span style="font-size:6px;color:var(--yellow)">ATM</span>':''}</td><td style="color:var(--orange)">${oiK(p.oi)}</td><td style="text-align:right;color:var(--red)">₹${p.ltp}<span class="oib" style="width:${pw}px;background:var(--orange)"></span></td></tr>`;}document.getElementById('oc-body').innerHTML=html;}
function renderSR(piv,spot){if(!piv||!spot)return;const f=n=>Math.round(n).toLocaleString('en-IN');const di=n=>{const d=Math.round(n-spot);return(d>=0?'+':'')+d;};const c=n=>n>spot?'var(--red)':n<spot?'var(--green)':'var(--yellow)';const lvls=[{n:'R3',v:piv.R3},{n:'R2',v:piv.R2},{n:'R1',v:piv.R1},null,{n:'S1',v:piv.S1},{n:'S2',v:piv.S2},{n:'S3',v:piv.S3},{n:'PVT',v:piv.P,sp:true}];document.getElementById('sr-list').innerHTML=lvls.map(l=>l?`<div class="sr-row${l.sp?' sr-spot':''}"><span class="sr-n" style="color:${l.sp?'var(--yellow)':'var(--muted)'}">${l.n}</span><span class="sr-v" style="color:${c(l.v)}">₹${f(l.v)}</span><span class="sr-d" style="color:${c(l.v)}">${di(l.v)}</span></div>`:`<div class="sr-row sr-spot"><span class="sr-n" style="color:var(--white);font-weight:700">SPOT</span><span class="sr-v" style="color:var(--white)">₹${f(spot)}</span><span class="sr-d" style="color:var(--muted)">current</span></div>`).join('');}

const NEWS=[{sev:'HIGH',src:'NSE',body:'BN weekly expiry — max pain tracking ATM'},{sev:'MED',src:'RBI',body:'RBI holds at 6.5% — neutral stance'},{sev:'HIGH',src:'ET Mkts',body:'FIIs sold ₹2,840 Cr in index futures'},{sev:'MED',src:'Reuters',body:'Crude near $68.50 ahead of OPEC+'},{sev:'HIGH',src:'Bloomberg',body:'Fed signals higher-for-longer — DXY rising'},];
(function(){const sC={HIGH:'var(--red)',MED:'var(--yellow)',LOW:'var(--teal)'};document.getElementById('news-list').innerHTML=NEWS.map(n=>`<div class="ni"><div class="ni-top"><span class="ni-sev" style="background:${sC[n.sev]}22;color:${sC[n.sev]}">${n.sev}</span><span class="ni-src">${n.src}</span></div><div class="ni-body">${n.body}</div></div>`).join('');})();

['rc-c','rc-e','rc-s'].forEach(id=>document.getElementById(id).addEventListener('input',()=>{const cap=parseFloat(document.getElementById('rc-c').value)||50000;const ent=parseFloat(document.getElementById('rc-e').value);const sl=parseFloat(document.getElementById('rc-s').value);if(!ent||!sl||ent<=sl){document.getElementById('rc-lots').textContent='—';document.getElementById('rc-mr').textContent='—';return;}const lots=Math.max(1,Math.min(10,Math.floor(cap*0.01/((ent-sl)*15))));document.getElementById('rc-lots').textContent=lots+(lots===1?' lot':' lots');document.getElementById('rc-mr').textContent='₹'+(lots*(ent-sl)*15).toLocaleString('en-IN');}));

function toast(m){document.getElementById('toast-msg').textContent=m;const t=document.getElementById('toast');t.classList.add('show');setTimeout(()=>t.classList.remove('show'),4500);}

const ARIA_SYS=`You are ARIA, Bank Nifty options trading assistant. Be brief (2-4 sentences), direct. No markdown.`;
async function callAria(msg){if(!ariaKey){ariaKey=prompt('Anthropic API key for ARIA\\n(console.anthropic.com)\\nLeave blank to skip:');if(!ariaKey)return'ARIA not connected.';}const ctx=`BN ₹${S.spot||'—'} | PCR ${S.pcr.toFixed(2)} | Signal: ${S.signal?.signal||'WAIT'} ${S.signal?.conf||0}% | RSI ${S.signal?.meta?.rsi||'—'}`;ariaHist.push({role:'user',content:`[${ctx}]\\n${msg}`});if(ariaHist.length>10)ariaHist=ariaHist.slice(-10);const res=await fetch('https://api.anthropic.com/v1/messages',{method:'POST',headers:{'Content-Type':'application/json','x-api-key':ariaKey,'anthropic-version':'2023-06-01','anthropic-dangerous-direct-browser-access':'true'},body:JSON.stringify({model:'claude-sonnet-4-6',max_tokens:200,system:ARIA_SYS,messages:ariaHist})});const d=await res.json();if(d.error)throw new Error(d.error.message);const r=d.content?.[0]?.text||'Sorry.';ariaHist.push({role:'assistant',content:r});return r;}
function addMsg(html,type){const box=document.getElementById('ai-msgs');const el=document.createElement('div');el.className='msg '+type;el.innerHTML=html;box.appendChild(el);box.scrollTop=box.scrollHeight;return el;}
async function sendAI(){const inp=document.getElementById('ai-in');const msg=inp.value.trim();if(!msg)return;inp.value='';document.getElementById('ai-btn').disabled=true;addMsg(msg,'user');const td=addMsg('<div class="typing"><span></span><span></span><span></span></div>','bot');try{const r=await callAria(msg);td.className='msg bot';td.innerHTML=r;}catch(e){td.className='msg bot';td.innerHTML='Error: '+e.message;}document.getElementById('ai-btn').disabled=false;}
function ariaExplain(sig){if(!sig||sig.signal==='WAIT')return;const isBuy=sig.signal==='BUY';addMsg(`${isBuy?'🟢':'🔴'} <strong>${isBuy?'BUY CALL':'BUY PUT'}</strong> — ${sig.strike} ${sig.otype} · ${sig.conf}% conf<br>Entry: ₹${sig.entry?.toLocaleString('en-IN')||'—'} · SL: ₹${sig.sl?.toLocaleString('en-IN')||'—'}`,'bot');}

// Real-time chart tick update
function updateChartTick(spot, high, low) {
  if (!cSeries || !lwC || !spot) return;
  try {
    const IST_OFFSET = 19800;
    // Get current minute timestamp in IST
    const now = Math.floor(Date.now() / 1000) + IST_OFFSET;
    const minuteTs = now - (now % 60);
    // Get last candle
    const last = window._lastCandle;
    if (!last) return;
    // Update current minute candle
    const updated = {
      time: minuteTs,
      open: last.time === minuteTs ? last.open : spot,
      high: last.time === minuteTs ? Math.max(last.high, high || spot) : spot,
      low: last.time === minuteTs ? Math.min(last.low, low || spot) : spot,
      close: spot,
    };
    window._lastCandle = updated;
    cSeries.update(updated);
    // Also update EMA/VWAP lines with new close
    if (e9S && window._chartCandles) {
      const candles = window._chartCandles;
      // Replace or append current candle
      const idx = candles.findIndex(c => c.time === minuteTs);
      if (idx >= 0) candles[idx] = {...updated, volume: candles[idx].volume || 0};
      else candles.push({...updated, volume: 0});
      const ema9 = calcEMA(candles, 9);
      const ema21 = calcEMA(candles, 21);
      const vwap = calcVWAP(candles);
      if (ema9.length) e9S.update(ema9[ema9.length-1]);
      if (ema21.length) e21S.update(ema21[ema21.length-1]);
      if (vwap.length) vwS.update(vwap[vwap.length-1]);
    }
  } catch(e) { /* silent */ }
}

// ═══════════════════ TRADE BRAIN v1 ═══════════════════
// Monitors open trades every tick, manages exits dynamically
// Detects reversals, trails stops, locks profits

const TradeBrain = {
  // State
  active: false,
  entry_spot: 0,
  direction: null,      // 'LONG' or 'SHORT'
  trail_sl: 0,
  highest_profit: 0,
  candles_in_trade: 0,
  last_check: 0,
  log: [],

  // Config
  config: {
    quick_exit_candles: 3,      // if wrong direction for 3 candles → exit
    trail_start_pts: 100,       // start trailing after 100pts profit
    trail_distance: 60,         // trail by 60pts
    reversal_ema_flip: true,    // exit if EMA9 flips against trade
    max_candles: 75,            // max 60 minutes in a trade
    profit_lock_pct: 0.5,       // lock 50% of profit when target1 hit
  },

  reset() {
    this.active = false;
    this.entry_spot = 0;
    this.direction = null;
    this.trail_sl = 0;
    this.highest_profit = 0;
    this.candles_in_trade = 0;
    this.log = [];
  },

  start(direction, entry_spot, initial_sl) {
    this.active = true;
    this.direction = direction;
    this.entry_spot = entry_spot;
    this.trail_sl = initial_sl;
    this.highest_profit = 0;
    this.candles_in_trade = 0;
    this.log = [`ENTERED ${direction} @ ${Math.round(entry_spot).toLocaleString('en-IN')}`];
    console.log('[BRAIN] Trade started:', direction, entry_spot);
  },

  // Called every tick with current market data
  tick(spot, cs, e9, e21, vwap, rsiVal) {
    if (!this.active) return null;

    this.candles_in_trade++;
    const pts = this.direction === 'LONG' ? spot - this.entry_spot : this.entry_spot - spot;
    const is_profit = pts > 0;

    // Track highest profit for trailing
    if (pts > this.highest_profit) {
      this.highest_profit = pts;
    }

    // ── EXIT CONDITIONS ──

    // 1. Max time in trade
    if (this.candles_in_trade >= this.config.max_candles) {
      return this._exit('TIME LIMIT - 60min max', spot);
    }

    // 2. Trailing stop loss
    if (this.highest_profit >= this.config.trail_start_pts) {
      const new_sl = this.direction === 'LONG'
        ? spot - this.config.trail_distance
        : spot + this.config.trail_distance;
      // Only move SL in profitable direction
      if (this.direction === 'LONG' && new_sl > this.trail_sl) {
        this.trail_sl = new_sl;
        this.log.push(`Trail SL → ${Math.round(new_sl).toLocaleString('en-IN')}`);
      }
      if (this.direction === 'SHORT' && new_sl < this.trail_sl) {
        this.trail_sl = new_sl;
        this.log.push(`Trail SL → ${Math.round(new_sl).toLocaleString('en-IN')}`);
      }
    }

    // 3. Stop loss hit (including trailing)
    const sl_hit = this.direction === 'LONG'
      ? spot <= this.trail_sl
      : spot >= this.trail_sl;
    if (sl_hit) {
      const reason = this.highest_profit >= this.config.trail_start_pts
        ? 'TRAIL STOP HIT' : 'STOP LOSS HIT';
      return this._exit(reason, spot);
    }

    // 4. EMA reversal detection
    if (this.config.reversal_ema_flip && this.candles_in_trade > 5) {
      const ema_against = this.direction === 'LONG' ? e9 < e21 : e9 > e21;
      const below_vwap_for_long = this.direction === 'LONG' && spot < vwap;
      const above_vwap_for_short = this.direction === 'SHORT' && spot > vwap;
      if (ema_against && (below_vwap_for_long || above_vwap_for_short) && pts < 0) {
        return this._exit('REVERSAL DETECTED - EMA+VWAP flipped', spot);
      }
    }

    // 5. Strong reversal candles (3 consecutive against trade)
    if (cs.length >= 3) {
      const last3 = cs.slice(-3);
      const all_against_long = this.direction === 'LONG' &&
        last3.every(c => c.c < c.o);  // 3 red candles
      const all_against_short = this.direction === 'SHORT' &&
        last3.every(c => c.c > c.o);  // 3 green candles
      if ((all_against_long || all_against_short) && pts < -30) {
        return this._exit('3 CONSECUTIVE REVERSAL CANDLES', spot);
      }
    }

    // 6. RSI extreme reversal
    if (this.direction === 'LONG' && rsiVal > 75 && pts > 50) {
      return this._exit('RSI OVERBOUGHT - TAKING PROFIT', spot);
    }
    if (this.direction === 'SHORT' && rsiVal < 25 && pts > 50) {
      return this._exit('RSI OVERSOLD - TAKING PROFIT', spot);
    }

    return { action: 'HOLD', pts, trail_sl: this.trail_sl, log: this.log };
  },

  _exit(reason, spot) {
    const pts = this.direction === 'LONG' ? spot - this.entry_spot : this.entry_spot - spot;
    this.log.push(`EXIT: ${reason} @ ${Math.round(spot).toLocaleString('en-IN')} (${pts>=0?'+':''}${Math.round(pts)}pts)`);
    console.log('[BRAIN] Exit:', reason, 'pts:', Math.round(pts));
    this.active = false;
    return { action: 'EXIT', reason, spot, pts, log: this.log };
  }
};

// ═══════════════════ MARKET BEHAVIOUR READER ═══════════════════
// Reads what market is doing RIGHT NOW and gives context

function readMarketBehaviour(cs, spot, vix, pcr) {
  if (cs.length < 10) return { behaviour: 'UNKNOWN', strength: 0, action: 'WAIT' };

  const cl = cs.map(c => c.c);
  const e9 = ema(cl, 9), e21 = ema(cl, 21);
  const rs = rsi(cl, 14);
  const vw = vwapCalc(cs);

  // Momentum: compare last 5 candles vs previous 5
  const recent5  = cl.slice(-5).reduce((a,b)=>a+b,0)/5;
  const prev5    = cl.slice(-10,-5).reduce((a,b)=>a+b,0)/5;
  const momentum = ((recent5 - prev5) / prev5) * 100;

  // Volume trend
  const recentVol = cs.slice(-5).reduce((a,c)=>a+c.volume,0)/5;
  const prevVol   = cs.slice(-10,-5).reduce((a,c)=>a+c.volume,0)/5;
  const volRising = recentVol > prevVol * 1.2;

  // Candle structure - are we seeing strong directional candles?
  const last5 = cs.slice(-5);
  const bullCandles = last5.filter(c => c.c > c.o && (c.c-c.o) > (c.h-c.l)*0.5).length;
  const bearCandles = last5.filter(c => c.c < c.o && (c.o-c.c) > (c.h-c.l)*0.5).length;

  // Distance from VWAP
  const vwapDist = ((spot - vw) / vw) * 100;

  let behaviour, strength, action, reason;

  // Strong trending up
  if (momentum > 0.3 && e9 > e21 && spot > vw && bullCandles >= 3) {
    behaviour = 'STRONG_UPTREND';
    strength = Math.min(100, 60 + bullCandles*8 + (volRising?15:0));
    action = 'BUY_CALL';
    reason = `Momentum +${momentum.toFixed(2)}% | ${bullCandles}/5 bull candles | Above VWAP`;
  }
  // Strong trending down
  else if (momentum < -0.3 && e9 < e21 && spot < vw && bearCandles >= 3) {
    behaviour = 'STRONG_DOWNTREND';
    strength = Math.min(100, 60 + bearCandles*8 + (volRising?15:0));
    action = 'BUY_PUT';
    reason = `Momentum ${momentum.toFixed(2)}% | ${bearCandles}/5 bear candles | Below VWAP`;
  }
  // Mild uptrend
  else if (momentum > 0.1 && e9 > e21 && spot > vw) {
    behaviour = 'MILD_UPTREND';
    strength = 45 + (volRising?10:0);
    action = 'WATCH_LONG';
    reason = `Mild upward momentum | EMA bull | Above VWAP`;
  }
  // Mild downtrend
  else if (momentum < -0.1 && e9 < e21 && spot < vw) {
    behaviour = 'MILD_DOWNTREND';
    strength = 45 + (volRising?10:0);
    action = 'WATCH_SHORT';
    reason = `Mild downward momentum | EMA bear | Below VWAP`;
  }
  // Reversal signals
  else if (spot > vw && rs > 70 && momentum < 0) {
    behaviour = 'POSSIBLE_REVERSAL_DOWN';
    strength = 55;
    action = 'WATCH_SHORT';
    reason = `RSI overbought ${rs} | Losing momentum`;
  }
  else if (spot < vw && rs < 30 && momentum > 0) {
    behaviour = 'POSSIBLE_REVERSAL_UP';
    strength = 55;
    action = 'WATCH_LONG';
    reason = `RSI oversold ${rs} | Gaining momentum`;
  }
  else {
    behaviour = 'RANGING';
    strength = 20;
    action = 'WAIT';
    reason = `No clear direction | Momentum ${momentum.toFixed(2)}%`;
  }

  return { behaviour, strength, action, reason, momentum, rs, vwapDist, e9, e21, vw };
}



function updateBrainDisplay(result){
  let el = document.getElementById('brain-status');
  if(!el) return;
  if(!result){el.textContent='';return;}
  const pts = result.pts ? Math.round(result.pts) : 0;
  el.textContent = 'BRAIN: '+(result.action||'HOLD')+' | '+pts+'pts | SL:'+Math.round(TradeBrain.trail_sl||0).toLocaleString('en-IN');
  el.style.color = pts>0?'var(--green)':pts<0?'var(--red)':'var(--muted)';
}
// Login alert system
let loginAlertShown = false;
function showLoginAlert(){
  if(loginAlertShown) return;
  loginAlertShown = true;
  // Show prominent alert
  let alert = document.getElementById('login-alert');
  if(!alert){
    alert = document.createElement('div');
    alert.id = 'login-alert';
    alert.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(6,10,16,0.95);z-index:500;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:30px';
    alert.innerHTML = '<div style="font-size:40px;margin-bottom:16px">🔐</div>'
      + '<div style="font-family:var(--cond);font-size:28px;font-weight:900;color:var(--orange);margin-bottom:10px">SESSION EXPIRED</div>'
      + '<div style="font-size:11px;color:var(--muted);margin-bottom:24px;line-height:1.8">Upstox token expired.<br>Login takes 10 seconds.</div>'
      + '<a href="/login" style="padding:14px 32px;background:var(--orange);color:#000;font-weight:900;font-size:16px;border-radius:4px;text-decoration:none;letter-spacing:0.08em;font-family:var(--cond)">LOGIN WITH UPSTOX →</a>'
      + '<div style="margin-top:16px;font-size:9px;color:var(--dim)">After login, come back to this page</div>';
    document.body.appendChild(alert);
  }
  alert.style.display = 'flex';
}

function hideLoginAlert(){
  loginAlertShown = false;
  const alert = document.getElementById('login-alert');
  if(alert) alert.style.display = 'none';
}

// Boot
fetchFromServer();setInterval(fetchFromServer,5000);
// Auto reload every 4 hours to prevent stale cache
setTimeout(()=>location.reload(), 4*60*60*1000);
// Auto reload page every 4 hours to prevent stale cache
setTimeout(()=>location.reload(), 4*60*60*1000);

// ═══════ UPSTOX CHART ENGINE ═══════
var lwC=null,cSeries=null,e9S=null,e21S=null,vwS=null,curIv=5;

function initChart(){
  if(typeof LightweightCharts==='undefined'){setTimeout(initChart,400);return;}
  var el=document.getElementById('lw_chart');
  if(!el||lwC)return;
  lwC=LightweightCharts.createChart(el,{
    width:el.clientWidth||window.innerWidth,height:320,
    layout:{background:{color:'#060A10'},textColor:'#4A6070'},
    grid:{vertLines:{color:'#192336'},horzLines:{color:'#192336'}},
    rightPriceScale:{borderColor:'#192336'},
    timeScale:{borderColor:'#192336',timeVisible:true,secondsVisible:false},
  });
  cSeries=lwC.addCandlestickSeries({upColor:'#00E676',downColor:'#FF1744',borderUpColor:'#00E676',borderDownColor:'#FF1744',wickUpColor:'#00E676',wickDownColor:'#FF1744'});
  e9S=lwC.addLineSeries({color:'#00BFA5',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
  e21S=lwC.addLineSeries({color:'#FF6D00',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
  vwS=lwC.addLineSeries({color:'#FFD600',lineWidth:1,lineStyle:1,priceLineVisible:false,lastValueVisible:false});
  lwC.subscribeCrosshairMove(function(p){
    if(!p.time)return;
    var d=p.seriesData&&p.seriesData.get(cSeries);
    if(!d)return;
    var leg=document.getElementById('chart-legend');
    if(leg)leg.textContent='O:'+Math.round(d.open).toLocaleString('en-IN')+'  H:'+Math.round(d.high).toLocaleString('en-IN')+'  L:'+Math.round(d.low).toLocaleString('en-IN')+'  C:'+Math.round(d.close).toLocaleString('en-IN');
  });
  window.addEventListener('resize',function(){if(lwC&&el)lwC.resize(el.clientWidth,el.clientHeight);});
  loadChart(1);
}

function calcEMA(data,p){var k=2/(p+1),e=data[0].close;return data.map(function(d,i){if(i>0)e=d.close*k+e*(1-k);return{time:d.time,value:+e.toFixed(2)};});}
function calcVWAP(data){var pv=0,v=0;return data.map(function(d){var tp=(d.high+d.low+d.close)/3;pv+=tp*(d.volume||1);v+=(d.volume||1);return{time:d.time,value:+(pv/v).toFixed(2)};});}

async function loadChart(iv){
  curIv=iv;
  document.querySelectorAll('.tf').forEach(function(b){var m={1:'1m',5:'5m',15:'15m',60:'1h'};b.classList.toggle('on',b.textContent===m[iv]);});
  var ld=document.getElementById('chart-loading');
  if(ld){ld.style.display='block';ld.textContent='LOADING CANDLES...';}
  try{
    var res=await fetchT('/api/candles?interval='+iv,12000);
    var data=await res.json();
    var c=data.candles||[];
    if(!c.length){if(ld)ld.textContent='Fetching candles... ('+data.error+')';setTimeout(()=>loadChart(iv),5000);return;}
    if(ld)ld.style.display='none';
    if(!lwC)initChart();
    setTimeout(function(){
      if(!cSeries)return;
      cSeries.setData(c);
      e9S.setData(calcEMA(c,9));
      e21S.setData(calcEMA(c,21));
      vwS.setData(calcVWAP(c));
      lwC.timeScale().fitContent();
      window._chartCandles = c.slice();
      window._lastCandle = c[c.length-1];
    },100);
  }catch(e){if(ld)ld.textContent='Error: '+e.message;}
}

// Load Lightweight Charts library
(function(){
  var s=document.createElement('script');
  s.src='https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js';
  s.onload=function(){setTimeout(initChart,200);};
  document.head.appendChild(s);
})();

// Auto-refresh chart every 30s when chart tab open
setInterval(function(){if(document.getElementById('page-1').classList.contains('on'))loadChart(1);},30000);


// PAPER TRADING ENGINE
let lastSignalFired = null;

async function fetchTrades(){
  try{
    const res = await fetch('/api/trades');
    const d = await res.json();
    renderTrades(d);
  }catch(e){}
  // Also fetch learning data
  try{
    const lr = await fetch('/api/learning');
    const ld = await lr.json();
    renderLearning(ld);
  }catch(e){}
}

function renderLearning(d){
  const daysEl = document.getElementById('learn-days');
  const bodyEl = document.getElementById('learn-body');
  if(!daysEl||!bodyEl||!d) return;
  if(!d.days_recorded){
    daysEl.textContent='0 days';
    bodyEl.innerHTML='<div style="text-align:center;color:var(--muted);font-size:10px;padding:10px">Learning data builds after market close each day</div>';
    return;
  }
  daysEl.textContent = d.days_recorded+' days recorded';
  const fc=n=>n>=0?'var(--green)':'var(--red)';
  const fp=n=>(n>=0?'+':'')+Math.round(Math.abs(n)).toLocaleString('en-IN');
  let html = '';
  // Overall stats
  html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:8px">'
    + '<div style="background:var(--bg3);border-radius:3px;padding:6px 8px"><div style="font-size:7px;color:var(--muted)">OVERALL WR</div><div style="font-family:var(--cond);font-size:16px;font-weight:900;color:'+(d.overall_win_rate>=50?'var(--green)':'var(--red)')+'">'+d.overall_win_rate+'%</div></div>'
    + '<div style="background:var(--bg3);border-radius:3px;padding:6px 8px"><div style="font-size:7px;color:var(--muted)">TOTAL TRADES</div><div style="font-family:var(--cond);font-size:16px;font-weight:900">'+d.total_trades+'</div></div>'
    + '<div style="background:var(--bg3);border-radius:3px;padding:6px 8px"><div style="font-size:7px;color:var(--muted)">TOTAL P&L</div><div style="font-family:var(--cond);font-size:16px;font-weight:900;color:'+fc(d.total_pnl)+'">'+fp(d.total_pnl)+'</div></div>'
    + '</div>';
  // Latest lessons
  if(d.latest_lessons&&d.latest_lessons.length){
    html += '<div style="margin-bottom:8px"><div style="font-size:7px;color:var(--muted);letter-spacing:0.1em;margin-bottom:4px">LATEST LESSONS</div>';
    d.latest_lessons.forEach(l=>{
      html += '<div style="font-size:9px;color:var(--white);padding:3px 0;border-bottom:1px solid var(--bdr)">\u2192 '+l+'</div>';
    });
    html += '</div>';
  }
  // Recent days
  if(d.recent_7_days&&d.recent_7_days.length){
    html += '<div style="font-size:7px;color:var(--muted);letter-spacing:0.1em;margin-bottom:4px">LAST '+Math.min(7,d.recent_7_days.length)+' DAYS</div>';
    d.recent_7_days.slice(0,5).forEach(day=>{
      const wr_col = day.win_rate>=50?'var(--green)':'var(--red)';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--bdr)">'
        + '<span style="font-size:8px;color:var(--muted)">'+day.date+'</span>'
        + '<span style="font-size:8px;color:var(--muted)">'+day.trades+' trades</span>'
        + '<span style="font-size:9px;font-weight:700;color:'+wr_col+'">'+day.win_rate+'%</span>'
        + '<span style="font-size:9px;font-weight:700;font-family:var(--cond);color:'+fc(day.total_pnl)+'">'+fp(day.total_pnl)+'</span>'
        + '<span style="font-size:7px;color:var(--muted)">VIX '+day.vix+'</span>'
        + '</div>';
    });
  }
  // Phase 3 pattern weights
  if(d.best_patterns&&d.best_patterns.length){
    html += '<div style="margin-top:8px;font-size:7px;color:var(--muted);letter-spacing:0.1em;margin-bottom:4px">PHASE 3 \u2014 LEARNED PATTERNS</div>';
    d.best_patterns.forEach(p=>{
      html += '<div style="font-size:8px;color:var(--green);padding:2px 0">\u2191 '+p[0]+' (weight: '+p[1].toFixed(2)+')</div>';
    });
    if(d.worst_patterns) d.worst_patterns.forEach(p=>{
      html += '<div style="font-size:8px;color:var(--red);padding:2px 0">\u2193 '+p[0]+' (weight: '+p[1].toFixed(2)+')</div>';
    });
  }
  bodyEl.innerHTML = html;
}

function renderTrades(d){
  if(!d) return;
  const f = n => Math.round(Math.abs(n||0)).toLocaleString('en-IN');
  const fp = n => (n>=0?'+':'-') + '₹' + f(n);
  const fc = n => n>=0?'var(--green)':'var(--red)';

  const availEl = document.getElementById('pt-avail');
  const pnlEl = document.getElementById('pt-pnl');
  const wrEl = document.getElementById('pt-wr');
  const cntEl = document.getElementById('pt-count');
  if(availEl) availEl.textContent = '₹' + f(d.available);
  if(pnlEl){ pnlEl.textContent = fp(d.stats.pnl); pnlEl.style.color = fc(d.stats.pnl); }
  const wr = d.stats.total > 0 ? Math.round(d.stats.wins/d.stats.total*100) : 0;
  if(wrEl){ wrEl.textContent = wr + '%'; wrEl.style.color = wr>=55?'var(--green)':wr>=45?'var(--yellow)':'var(--red)'; }
  if(cntEl) cntEl.textContent = d.stats.total + ' trades';

  const body = document.getElementById('pt-open-body');
  const timeEl = document.getElementById('pt-open-time');
  if(body){
    if(d.open_trade){
      const t = d.open_trade;
      const isBuy = t.otype === 'CE';
      const col = isBuy ? 'var(--green)' : 'var(--red)';
      const lp = d.live_pnl || 0;
      if(timeEl) timeEl.textContent = ' | ' + t.time;
      body.innerHTML = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--bdr)">'
        + '<div style="background:var(--bg2);padding:10px 12px"><div style="font-size:7px;color:var(--muted)">SIGNAL</div><div style="font-family:var(--cond);font-size:22px;font-weight:900;color:'+col+'">'+(isBuy?'BUY CALL':'BUY PUT')+'</div></div>'
        + '<div style="background:var(--bg2);padding:10px 12px"><div style="font-size:7px;color:var(--muted)">STRIKE</div><div style="font-family:var(--cond);font-size:22px;font-weight:900">'+t.strike.toLocaleString('en-IN')+' '+t.otype+'</div></div>'
        + '<div style="background:var(--bg2);padding:10px 12px"><div style="font-size:7px;color:var(--muted)">ENTRY PREMIUM</div><div style="font-family:var(--cond);font-size:18px;font-weight:900">₹'+t.entry_premium+'</div></div>'
        + '<div style="background:var(--bg2);padding:10px 12px"><div style="font-size:7px;color:var(--muted)">LIVE P&L</div><div style="font-family:var(--cond);font-size:18px;font-weight:900;color:'+fc(lp)+'">'+fp(lp)+'</div></div>'
        + '<div style="background:var(--bg2);padding:10px 12px"><div style="font-size:7px;color:var(--muted)">LOTS / QTY</div><div style="font-family:var(--cond);font-size:18px;font-weight:900">'+t.lots+' lot / '+t.qty+'</div></div>'
        + '<div style="background:var(--bg2);padding:10px 12px"><div style="font-size:7px;color:var(--muted)">ENTRY SPOT</div><div style="font-family:var(--cond);font-size:18px;font-weight:900">₹'+t.entry_spot.toLocaleString('en-IN')+'</div></div>'
        + '</div>'
        + '<div style="display:flex;gap:1px;background:var(--bdr);margin-top:1px">'
        + '<div style="flex:1;background:var(--bg2);padding:8px 12px;border-left:3px solid var(--red)"><div style="font-size:7px;color:var(--muted)">STOP LOSS</div><div style="font-family:var(--cond);font-size:16px;font-weight:900;color:var(--red)">₹'+t.sl.toLocaleString('en-IN')+'</div></div>'
        + '<div style="flex:1;background:var(--bg2);padding:8px 12px;border-left:3px solid var(--teal)"><div style="font-size:7px;color:var(--muted)">TARGET 1</div><div style="font-family:var(--cond);font-size:16px;font-weight:900;color:var(--teal)">₹'+t.t1.toLocaleString('en-IN')+'</div></div>'
        + '<div style="flex:1;background:var(--bg2);padding:8px 12px;border-left:3px solid var(--green)"><div style="font-size:7px;color:var(--muted)">TARGET 2</div><div style="font-family:var(--cond);font-size:16px;font-weight:900;color:var(--green)">₹'+t.t2.toLocaleString('en-IN')+'</div></div>'
        + '</div>';
    } else {
      if(timeEl) timeEl.textContent = '';
      body.innerHTML = '<div style="text-align:center;color:var(--muted);font-size:10px;padding:16px">No open position — waiting for signal</div>';
    }
  }

  const hist = document.getElementById('pt-history');
  if(hist){
    if(!d.trades || !d.trades.length){
      hist.innerHTML = '<div style="text-align:center;color:var(--muted);font-size:10px;padding:20px">No trades yet</div>';
    } else {
      hist.innerHTML = d.trades.map(function(t){
        const isBuy = t.otype==='CE';
        const col = t.pnl>=0?'var(--green)':'var(--red)';
        const bg = t.pnl>=0?'rgba(0,230,118,0.05)':'rgba(255,23,68,0.05)';
        return '<div style="padding:10px 12px;border-bottom:1px solid var(--bdr);background:'+bg+'">'
          + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">'
          + '<span style="font-family:var(--cond);font-size:13px;font-weight:900;color:'+(isBuy?'var(--green)':'var(--red)')+'">'+  (isBuy?'BUY CALL':'BUY PUT')+' '+t.strike+'</span>'
          + '<span style="font-family:var(--cond);font-size:15px;font-weight:900;color:'+col+'">'+fp(t.pnl)+'</span>'
          + '</div>'
          + '<div style="font-size:8px;color:var(--muted)">'+t.time+' → '+(t.exit_time||'-')+' · '+t.lots+' lot · Entry ₹'+t.entry_premium+' → ₹'+(t.exit_premium||'-')+' · <span style="color:'+col+'">'+t.reason+'</span></div>'
          + '</div>';
      }).join('');
    }
  }
}

async function manualClose(){
  if(!confirm('Close open trade now?')) return;
  await fetch('/api/trades/close', {method:'POST'});
  fetchTrades();
}

async function resetAccount(){
  if(!confirm('Reset all trades and start fresh with \u20b91,00,000?')) return;
  await fetch('/api/trades/reset', {method:'POST'});
  fetchTrades();
}

function checkPaperTrade(sig){
  if(!sig || sig.signal==='WAIT') return;
  const now5min = Math.floor(Date.now()/300000); // changes every 5 minutes
  const key = sig.signal+'_'+sig.strike+'_'+now5min;
  if(key === lastSignalFired) return;
  const effectiveConf = sig.conf || (sig.signal!=='WAIT' ? 65 : 0);
  if(effectiveConf >= 55){
    lastSignalFired = key;
    fetch('/api/trades/signal', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({signal:sig.signal==='BUY'?'BUY':'SELL', otype:sig.otype, strike:sig.strike, sl:sig.sl, t1:sig.t1, t2:sig.t2, conf:effectiveConf})
    }).then(function(){
      fetchTrades();
      // Start TradeBrain monitoring
      const direction = sig.signal==='BUY'?'LONG':'SHORT';
      TradeBrain.start(direction, S.spot, sig.sl||S.spot);
      toast('Trade opened: '+(sig.signal==='BUY'?'BUY CALL':'BUY PUT')+' '+sig.strike.toLocaleString('en-IN')+' @ '+sig.conf+'% | Brain active');
    });
  }
}

setInterval(fetchTrades, 5000);
fetchTrades();

</script>
</body>
</html>
"""

def save_token(data):
    state["access_token"] = data.get("access_token", "")
    with open(TOKEN_FILE, "w") as f: json.dump(data, f)
    cache["authenticated"] = True
    print("[AUTH] Token saved.")

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
            state["access_token"] = data.get("access_token", "")
            if state["access_token"]:
                cache["authenticated"] = True
                print("[AUTH] Token loaded.")
                return True
    except: pass
    return False

def hdr():
    return {"Authorization": f"Bearer {state['access_token']}", "Accept": "application/json"}

@app.route("/login")
def login():
    params = {"client_id": API_KEY, "redirect_uri": REDIRECT_URI,
              "response_type": "code", "state": "bn_terminal"}
    return redirect("https://api.upstox.com/v2/login/authorization/dialog?" + urlencode(params))

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code: return "Error: No code.", 400
    try:
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            data={"code": code, "client_id": API_KEY, "client_secret": API_SECRET,
                  "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        )
        data = resp.json()
        if "access_token" not in data: return f"Auth failed: {data}", 400
        save_token(data)
        fetch_prices()
        # Start fetch loop if not running
        t = threading.Thread(target=fetch_loop, daemon=True)
        t.daemon = True
        t.start()
        if WS_AVAILABLE:
            threading.Thread(target=start_websocket, daemon=True).start()
            print("[WS] WebSocket thread started")
        else:
            print("[WS] websocket-client not available - using REST polling only")
        return """<html><body style='background:#060A10;color:#00E676;font-family:monospace;padding:40px;text-align:center'>
        <h1 style='color:#FF6D00;font-size:32px'>✓ CONNECTED</h1>
        <p style='font-size:16px;color:#D8E8F8'>Upstox authenticated!<br><br>
        <a href='/terminal' style='padding:14px 28px;background:#FF6D00;color:#000;font-weight:900;font-size:16px;border-radius:4px;text-decoration:none'>OPEN TERMINAL →</a></p>
        <p style='color:#4A6070;font-size:11px;margin-top:20px'>Bookmark /terminal — open it anytime on any device.</p>
        </body></html>"""
    except Exception as e:
        return f"Error: {e}", 500

@app.route("/terminal")
def terminal():
    return Response(TERMINAL_HTML, mimetype="text/html")

def fetch_quote(keys):
    try:
        url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={requests.utils.quote(','.join(keys))}"
        r = requests.get(url, headers=hdr(), timeout=6)
        r.raise_for_status()
        return r.json().get("data", {})
    except Exception as e:
        print(f"[QUOTE] {e}"); return {}

def fetch_option_chain(spot):
    try:
        d = date.today()
        days = (3 - d.weekday()) % 7
        if days == 0: days = 7
        expiry = (d + timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"https://api.upstox.com/v2/option/chain?instrument_key={requests.utils.quote(BN_KEY)}&expiry_date={expiry}"
        r = requests.get(url, headers=hdr(), timeout=10)
        if r.status_code != 200: return None
        data = r.json().get("data", [])
        if not data: return None
        chain, tot_ce, tot_pe, pain = [], 0, 0, {}
        for item in data:
            s = item.get("strike_price", 0)
            ce = item.get("call_options", {}).get("market_data", {})
            pe = item.get("put_options",  {}).get("market_data", {})
            co, po = ce.get("oi", 0) or 0, pe.get("oi", 0) or 0
            tot_ce += co; tot_pe += po
            chain.append({"strike": s, "ce_ltp": round(ce.get("ltp",0) or 0,1),
                "ce_oi": co, "ce_iv": round(ce.get("iv",0) or 0,1),
                "pe_ltp": round(pe.get("ltp",0) or 0,1),
                "pe_oi": po, "pe_iv": round(pe.get("iv",0) or 0,1),
                "is_atm": abs(s-spot)<50})
            pain[s] = sum(max(0,ss.get("strike_price",0)-s)*(ss.get("call_options",{}).get("market_data",{}).get("oi",0) or 0)
                        + max(0,s-ss.get("strike_price",0))*(ss.get("put_options",{}).get("market_data",{}).get("oi",0) or 0)
                        for ss in data)
        max_pain = min(pain, key=pain.get) if pain else round(spot/100)*100
        pcr = round(tot_pe/tot_ce, 2) if tot_ce > 0 else 1.0
        chain.sort(key=lambda x: x["strike"])
        idx = min(range(len(chain)), key=lambda i: abs(chain[i]["strike"]-spot))
        chain = chain[max(0,idx-6):idx+7]
        return {"chain": chain, "pcr": pcr, "max_pain": max_pain, "tot_ce_oi": tot_ce, "tot_pe_oi": tot_pe}
    except Exception as e:
        print(f"[OC] {e}"); return None

def fetch_globals():
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=%5EGSPC,CL%3DF,GC%3DF,USDINR%3DX"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        q = {x["symbol"]: x for x in r.json().get("quoteResponse", {}).get("result", [])}
        cache["sp500_chg"] = round(q.get("^GSPC",   {}).get("regularMarketChangePercent", 0), 2)
        cache["crude_chg"] = round(q.get("CL=F",    {}).get("regularMarketChangePercent", 0), 2)
        cache["gold_chg"]  = round(q.get("GC=F",    {}).get("regularMarketChangePercent", 0), 2)
        cache["usdinr"]    = round(q.get("USDINR=X",{}).get("regularMarketPrice", 0), 2)
    except Exception as e:
        print(f"[GLOBALS] {e}")


def resample_candles(candles_1m, target_minutes):
    """Resample 1min candles into larger timeframes."""
    if target_minutes == 1:
        return candles_1m
    resampled = []
    bucket = []
    for c in candles_1m:
        bucket.append(c)
        if len(bucket) >= target_minutes:
            resampled.append({
                "time":   bucket[0]["time"],
                "open":   bucket[0]["open"],
                "high":   max(x["high"] for x in bucket),
                "low":    min(x["low"]  for x in bucket),
                "close":  bucket[-1]["close"],
                "volume": sum(x["volume"] for x in bucket),
            })
            bucket = []
    if bucket:  # leftover partial candle
        resampled.append({
            "time":   bucket[0]["time"],
            "open":   bucket[0]["open"],
            "high":   max(x["high"] for x in bucket),
            "low":    min(x["low"]  for x in bucket),
            "close":  bucket[-1]["close"],
            "volume": sum(x["volume"] for x in bucket),
        })
    return resampled

def fetch_candles(interval='5'):
    """Fetch OHLCV candle data from Upstox. Always fetch 1min, resample as needed."""
    try:
        from datetime import timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(timezone.utc).astimezone(ist).strftime('%Y-%m-%d')
        yesterday = (datetime.now(timezone.utc).astimezone(ist) - timedelta(days=5)).strftime('%Y-%m-%d')
        target_min = int({'1':1,'5':5,'15':15,'60':30}.get(str(interval), 5))
        # Upstox only supports 1minute and 30minute
        upstox_iv = '1minute'

        raw_candles = []

        # Try intraday first (market hours - today)
        url = (f"https://api.upstox.com/v2/historical-candle/intraday/"
               f"{requests.utils.quote(BN_KEY)}/{upstox_iv}")
        r = requests.get(url, headers=hdr(), timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", {}).get("candles", [])
            if data:
                for c in data:
                    try:
                        ts = int(datetime.fromisoformat(c[0].replace('Z','+00:00')).timestamp())
                        raw_candles.append({"time": ts, "open": float(c[1]), "high": float(c[2]),
                                           "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])})
                    except: pass
                raw_candles.sort(key=lambda x: x["time"])
                print(f"[CANDLES] Intraday 1min: {len(raw_candles)} candles")

        # Fallback: historical
        if not raw_candles:
            url2 = (f"https://api.upstox.com/v2/historical-candle/"
                    f"{requests.utils.quote(BN_KEY)}/{upstox_iv}/{today}/{yesterday}")
            r2 = requests.get(url2, headers=hdr(), timeout=15)
            if r2.status_code == 200:
                data2 = r2.json().get("data", {}).get("candles", [])
                for c in data2:
                    try:
                        ts = int(datetime.fromisoformat(c[0].replace('Z','+00:00')).timestamp())
                        raw_candles.append({"time": ts, "open": float(c[1]), "high": float(c[2]),
                                           "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])})
                    except: pass
                raw_candles.sort(key=lambda x: x["time"])
                print(f"[CANDLES] Historical 1min: {len(raw_candles)} candles")
            else:
                print(f"[CANDLES] Historical failed: {r2.status_code} {r2.text[:200]}")

        if not raw_candles:
            return []

        # Resample to target interval
        result = resample_candles(raw_candles, target_min)
        print(f"[CANDLES] Resampled to {interval}min: {len(result)} candles")
        return result

    except Exception as e:
        print(f"[CANDLES] Error: {e}")
        return []

# Candle cache
candle_cache = {"1": [], "5": [], "15": [], "60": [], "last_fetch": {}}

def refresh_candles(interval='5'):
    """Refresh candle cache for given interval."""
    data = fetch_candles(str(interval))
    if data:
        candle_cache[str(interval)] = data
        candle_cache["last_fetch"][str(interval)] = datetime.now().strftime("%H:%M:%S")
    return data



def fetch_prices():
    if not state["access_token"]:
        cache["error"] = "Not authenticated - visit /login"; cache["source"] = "unauthenticated"; return False
    # Check if token might be expired (Upstox tokens expire daily at midnight IST)
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(timezone.utc).astimezone(ist)
    # If it's past 9am and we have no spot price and we're authenticated - likely expired
    if (cache.get("spot", 0) == 0 and cache.get("source") == "error" and
        ist_now.hour >= 9 and cache.get("authenticated")):
        cache["error"] = "Token may be expired - please visit /login to refresh"
    try:
        data = fetch_quote([BN_KEY, VIX_KEY])
        bn  = data.get("NSE_INDEX:Nifty Bank", {})
        vix = data.get("NSE_INDEX:India VIX", {})
        if not bn: raise ValueError("No BN data")
        spot = bn.get("last_price", 0)
        if not spot or spot < 30000 or spot > 110000: raise ValueError(f"Bad price: {spot}")
        ohlc = bn.get("ohlc", {}) or {}
        def safe(v, default=0): 
            try: return round(float(v),2) if v is not None else default
            except: return default
        cache.update({
            "spot": safe(spot), "change": safe(bn.get("net_change")),
            "pct":  safe(bn.get("change_percentage")),
            "high": safe(ohlc.get("high"), safe(spot)), "low": safe(ohlc.get("low"), safe(spot)),
            "open": safe(ohlc.get("open"), safe(spot)),
            "vwap": safe(bn.get("average_price"), safe(spot)),
            "vix":  safe(vix.get("last_price") if vix else 0),
            "last_updated": datetime.now().strftime("%H:%M:%S"),
            "source": "Upstox Live ✓", "error": "", "authenticated": True,
        })
        oc = fetch_option_chain(spot)
        if oc:
            cache.update({"option_chain": oc["chain"], "pcr": oc["pcr"],
                "max_pain": oc["max_pain"], "tot_ce_oi": oc["tot_ce_oi"], "tot_pe_oi": oc["tot_pe_oi"]})
        fetch_globals()
        cache["market_open"] = True
        save_last_session()
        # Check paper trade exits on every price update
        if paper["open_trade"] and spot:
            check_exit(spot)
        # Refresh candles every 2 minutes (not every tick)
        if int(time.time()) % 120 < 6:
            threading.Thread(target=refresh_candles, args=("1",), daemon=True).start()
        print(f"[{cache['last_updated']}] BN ₹{spot:,.0f} | VIX {cache['vix']} | PCR {cache['pcr']}")
        return True
    except Exception as e:
        cache["error"] = str(e)
        cache["market_open"] = False
        # If market is closed, use last session data as display fallback
        if cache["last_session"]:
            cache["source"] = f"Last session: {cache['last_session'].get('saved_at','?')}"
        else:
            cache["source"] = "error"
        print(f"[FETCH] {e}"); return False

def parse_upstox_tick(data):
    """Parse Upstox WebSocket protobuf tick data."""
    try:
        # Upstox v2 WebSocket sends protobuf - we use a simple binary parser
        # The key fields are at known offsets for LTPC feed type
        import struct
        # Try to extract last price from protobuf
        # Field 1 (feeds map), then nested fields
        # Simpler: use the REST fallback but trigger it on tick arrival
        return None
    except:
        return None

def on_ws_message(ws, message):
    """Called when WebSocket receives a message."""
    try:
        ws_state["last_tick"] = time.time()
        # Upstox sends protobuf binary - trigger a REST fetch for clean data
        # This gives us ~0.5-1 sec delay (WS wakes us up, REST gives clean data)
        if cache["authenticated"] and is_market_open():
            threading.Thread(target=fetch_prices, daemon=True).start()
    except Exception as e:
        print(f"[WS] Message error: {e}")

def on_ws_open(ws):
    """Called when WebSocket connects."""
    print("[WS] Connected to Upstox market feed")
    ws_state["connected"] = True
    ws_state["reconnect_count"] = 0
    # Subscribe to Bank Nifty live feed
    sub_msg = json.dumps({
        "guid": "bn_terminal_feed",
        "method": "sub",
        "data": {
            "mode": "ltpc",
            "instrumentKeys": [BN_KEY, VIX_KEY]
        }
    })
    ws.send(sub_msg)
    print("[WS] Subscribed to BANKNIFTY + VIX")

def on_ws_error(ws, error):
    print(f"[WS] Error: {error}")
    ws_state["connected"] = False

def on_ws_close(ws, code, msg):
    print(f"[WS] Closed: {code} {msg}")
    ws_state["connected"] = False

def start_websocket():
    """Start Upstox WebSocket connection with auto-reconnect."""
    while True:
        if not state["access_token"] or not is_market_open():
            time.sleep(10)
            continue
        try:
            ws_state["reconnect_count"] += 1
            print(f"[WS] Connecting... (attempt {ws_state['reconnect_count']})")
            ws_url = "wss://api.upstox.com/v2/feed/market-data-feed"
            ws_app = websocket.WebSocketApp(
                ws_url,
                header={"Authorization": f"Bearer {state['access_token']}"},
                on_open=on_ws_open,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close,
            )
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print(f"[WS] Connection failed: {e}")
        ws_state["connected"] = False
        time.sleep(5)  # Wait before reconnecting

def load_historical_for_display():
    """When market is closed, fetch yesterday's data so terminal shows something useful."""
    try:
        from datetime import timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(ist)
        # Get last trading day (skip weekends)
        offset = 1
        while True:
            last_day = today - timedelta(days=offset)
            if last_day.weekday() < 5:  # Mon-Fri
                break
            offset += 1
        last_str = last_day.strftime('%Y-%m-%d')
        start_str = (last_day - timedelta(days=1)).strftime('%Y-%m-%d')
        print(f"[HISTORICAL] Loading data for {last_str}")
        # Fetch yesterday's quote from historical endpoint
        url = (f"https://api.upstox.com/v2/historical-candle/"
               f"{requests.utils.quote(BN_KEY)}/day/{last_str}/{start_str}")
        r = requests.get(url, headers=hdr(), timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", {}).get("candles", [])
            if data:
                c = data[0]  # Most recent day
                spot = float(c[4])  # close price
                hi = float(c[2])
                lo = float(c[3])
                op = float(c[1])
                vw = (hi + lo + spot) / 3
                saved_at = f"{last_str} (Previous Close)"
                # Update cache with historical data for display
                if not cache["last_session"]:
                    cache["last_session"] = {
                        "spot": spot, "change": 0, "pct": 0,
                        "high": hi, "low": lo, "open": op, "vwap": vw,
                        "vix": 0, "pcr": 1.0, "max_pain": round(spot/100)*100,
                        "tot_ce_oi": 0, "tot_pe_oi": 0,
                        "sp500_chg": 0, "crude_chg": 0, "gold_chg": 0, "usdinr": 0,
                        "option_chain": [], "last_updated": last_str,
                        "saved_at": saved_at
                    }
                    print(f"[HISTORICAL] Loaded: BN ₹{spot:,.0f} on {last_str}")
        # Also load candles for chart display
        url2 = (f"https://api.upstox.com/v2/historical-candle/"
                f"{requests.utils.quote(BN_KEY)}/1minute/{last_str}/{start_str}")
        r2 = requests.get(url2, headers=hdr(), timeout=15)
        if r2.status_code == 200:
            raw = r2.json().get("data", {}).get("candles", [])
            candles = []
            for c in raw:
                try:
                    ts = int(datetime.fromisoformat(c[0].replace('Z','+00:00')).timestamp())
                    candles.append({"time": ts, "open": float(c[1]), "high": float(c[2]),
                                    "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])})
                except: pass
            candles.sort(key=lambda x: x["time"])
            if candles:
                candle_cache["5"] = candles
                print(f"[HISTORICAL] Loaded {len(candles)} candles for chart")
    except Exception as e:
        print(f"[HISTORICAL] Error: {e}")

def fetch_loop():
    was_open = False
    historical_loaded = False
    consecutive_failures = 0
    while True:
        try:
            now_open = is_market_open()
            cache["market_open"] = now_open
            if now_open:
                was_open = True
                historical_loaded = False
                success = fetch_prices()
                if success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    print(f"[LOOP] Fetch failed ({consecutive_failures} times)")
                    if consecutive_failures >= 10:
                        # Token likely expired - mark as unauthenticated
                        print("[LOOP] Too many failures - token may have expired")
                        cache["authenticated"] = False
                        cache["source"] = "Token expired - please login again"
                        consecutive_failures = 0
                time.sleep(5)
            else:
                cache['market_open'] = False
                consecutive_failures = 0
                if was_open:
                    print("[SESSION] Market closed. Saving final session.")
                    save_last_session()
                    was_open = False
                if not historical_loaded:
                    print("[SESSION] Market closed — loading historical data.")
                    load_historical_for_display()
                    historical_loaded = True
                time.sleep(300)
        except Exception as e:
            print(f"[LOOP] Unexpected error: {e}")
            time.sleep(10)  # Wait and retry

@app.route("/")
def index():
    if not cache["authenticated"]:
        return """<html><body style='background:#060A10;color:#D8E8F8;font-family:monospace;padding:40px;text-align:center'>
        <h1 style='color:#FF6D00;font-size:28px;letter-spacing:0.1em'>BN TERMINAL SERVER</h1>
        <p style='color:#4A6070;margin:10px 0 30px'>One-time Upstox login required</p>
        <a href='/login' style='padding:14px 32px;background:#FF6D00;color:#000;font-weight:900;font-size:16px;border-radius:4px;text-decoration:none;letter-spacing:0.08em'>LOGIN WITH UPSTOX →</a>
        </body></html>"""
    return redirect("/terminal")

@app.route("/api/price")
def price_api():
    # When market closed, merge last session into response for display
    response = dict(cache)
    if not cache["market_open"] and cache["last_session"]:
        ls = cache["last_session"]
        for key in ["spot","change","pct","high","low","open","vwap",
                    "vix","pcr","max_pain","tot_ce_oi","tot_pe_oi",
                    "sp500_chg","crude_chg","gold_chg","usdinr","option_chain"]:
            if response.get(key, 0) == 0 and ls.get(key):
                response[key] = ls[key]
        response["using_last_session"] = True
        response["last_session_time"] = ls.get("saved_at", "")
    return jsonify(response)

@app.route("/api/status")
def status(): return jsonify({"running": True, "authenticated": cache["authenticated"],
    "spot": cache["spot"], "last_updated": cache["last_updated"], "source": cache["source"]})


@app.route("/api/candles")
def candles_api():
    interval = request.args.get("interval", "5")
    cached = candle_cache.get(str(interval), [])
    if not cached or cache.get("market_open"):
        fresh = fetch_candles(str(interval))
        if fresh:
            candle_cache[str(interval)] = fresh
            cached = fresh
    # Convert UTC timestamps to IST (+5:30 = +19800 seconds) for chart display
    IST_OFFSET = 19800
    candles_ist = [{**c, "time": c["time"] + IST_OFFSET} for c in cached]
    return jsonify({"candles": candles_ist, "interval": interval, "count": len(candles_ist)})

@app.route("/ping")
def ping(): return "pong"

@app.route("/api/learning")
def learning_api():
    """Phase 2 - Return learning summary and daily performance."""
    return jsonify(get_learning_summary())

@app.route("/api/learning/day")
def learning_day():
    """Get today's learning review manually."""
    try:
        review = end_of_day_review()
        return jsonify(review or {"status": "No trades today"})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/trades")
def trades_api():
    spot = cache.get("spot", 0)
    open_t = paper["open_trade"]
    live_pnl = 0
    if open_t and spot:
        curr_prem = estimate_premium(spot, open_t["strike"], open_t["otype"], cache.get("vix",15))
        live_pnl = round((curr_prem - open_t["entry_premium"]) * open_t["qty"], 2)
    return jsonify({
        "capital": paper["capital"],
        "available": round(paper["available"], 2),
        "open_trade": open_t,
        "live_pnl": live_pnl,
        "trades": paper["trades"][:50],
        "stats": paper["stats"],
        "daily": paper["daily"],
        "rules": RULES
    })

@app.route("/api/trades/signal", methods=["POST"])
def trades_signal():
    """Called when a new signal fires — open a paper trade."""
    data = request.json or {}
    sig = data.get("signal")
    spot = cache.get("spot", 0)
    vix = cache.get("vix", 15)
    conf = data.get('conf', 65)
    otype = data.get('otype', 'CE')
    # Phase 3: Apply adaptive confidence boost
    boost = get_adaptive_confidence_boost(otype, vix)
    adjusted_conf = conf + boost
    data['conf'] = adjusted_conf
    data['phase3_boost'] = boost
    print(f"[PAPER] Signal: {sig} | Conf: {conf}% + boost {boost:+d}% = {adjusted_conf}% | Spot: {spot}")
    if sig in ("BUY", "SELL") and spot:
        open_trade(data, spot, vix)
    else:
        print(f"[PAPER] Signal ignored: sig={sig} spot={spot}")
    return jsonify({"ok": True})

@app.route("/api/trades/close", methods=["POST"])
def trades_close():
    """Manually close open trade."""
    spot = cache.get("spot", 0)
    close_trade(spot, "MANUAL CLOSE")
    return jsonify({"ok": True})

@app.route("/api/trades/close_reason", methods=["POST"])
def trades_close_reason():
    """Close trade with specific reason from TradeBrain."""
    data = request.json or {}
    spot = data.get("spot", cache.get("spot", 0))
    reason = data.get("reason", "BRAIN EXIT")
    close_trade(spot, reason)
    return jsonify({"ok": True})

@app.route("/api/trades/reset", methods=["POST"])
def trades_reset():
    """Reset paper trading account."""
    paper["capital"] = 100000
    paper["available"] = 100000
    paper["open_trade"] = None
    paper["trades"] = []
    paper["stats"] = {"total":0,"wins":0,"losses":0,"pnl":0}
    save_trades()
    return jsonify({"ok": True})

@app.route("/api/stream")
def stream():
    """SSE endpoint - browser connects here for real-time push updates."""
    def event_stream():
        q = queue.Queue(maxsize=50)
        with sse_lock:
            sse_clients.append(q)
        try:
            # Send current state immediately
            data = json.dumps({k: cache[k] for k in ["spot","change","pct","high","low","open",
                "vwap","vix","pcr","max_pain","sp500_chg","crude_chg","gold_chg","usdinr",
                "last_updated","source","market_open","authenticated"]
                if k in cache})
            yield f"data: {data}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)
    return Response(stream_with_context(event_stream()),
                   mimetype="text/event-stream",
                   headers={"Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no",
                            "Connection": "keep-alive"})

@app.route("/api/candles/debug")
def candles_debug():
    """Debug endpoint to see raw Upstox candle response."""
    try:
        from datetime import timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(ist).strftime("%Y-%m-%d")
        yesterday = (datetime.now(ist) - timedelta(days=2)).strftime("%Y-%m-%d")
        # Try intraday
        url1 = f"https://api.upstox.com/v2/historical-candle/intraday/{requests.utils.quote(BN_KEY)}/1minute"
        r1 = requests.get(url1, headers=hdr(), timeout=10)
        # Try historical
        url2 = f"https://api.upstox.com/v2/historical-candle/{requests.utils.quote(BN_KEY)}/1minute/{today}/{yesterday}"
        r2 = requests.get(url2, headers=hdr(), timeout=10)
        return jsonify({
            "intraday_status": r1.status_code,
            "intraday_sample": r1.json() if r1.status_code==200 else r1.text[:300],
            "historical_status": r2.status_code,
            "historical_sample": str(r2.json())[:300] if r2.status_code==200 else r2.text[:300],
            "today": today,
            "yesterday": yesterday,
            "authenticated": cache["authenticated"]
        })
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    print("=" * 50)
    print("  BN TERMINAL — UPSTOX ALL-IN-ONE SERVER")
    print("=" * 50)
    load_token()
    load_last_session()
    load_trades()
    load_learning()
    if cache["authenticated"]:
        if is_market_open():
            print("Market open — fetching live prices...")
            fetch_prices()
        else:
            print("Market closed — loading historical data...")
            load_historical_for_display()
        threading.Thread(target=fetch_loop, daemon=True).start()
        if WS_AVAILABLE:
            threading.Thread(target=start_websocket, daemon=True).start()
            print("[WS] WebSocket thread started")
        else:
            print("[WS] websocket-client not available - using REST polling only")
    else:
        print("Open http://localhost:5000 → Login with Upstox")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
