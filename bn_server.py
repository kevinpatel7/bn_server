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
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
            paper["trades"] = data.get("trades", [])
            paper["stats"] = data.get("stats", {"total":0,"wins":0,"losses":0,"pnl":0})
            paper["available"] = data.get("available", 100000)
            paper["open_trade"] = data.get("open_trade", None)
            print(f"[TRADES] Loaded {len(paper['trades'])} trades")
    except: pass

def save_trades():
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump({"trades": paper["trades"], "stats": paper["stats"],
                       "available": paper["available"], "open_trade": paper["open_trade"]}, f)
    except Exception as e:
        print(f"[TRADES] Save error: {e}")

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

TERMINAL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BN Terminal</title>
<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#060A10;--bg2:#0D1117;--bg3:#111820;--dim:#1A2233;
  --bdr:#1E2D45;--muted:#4A6080;--white:#E8F0FF;
  --green:#00E676;--red:#FF1744;--yellow:#FFD600;
  --orange:#FF6D00;--teal:#00BFA5;--blue:#2979FF;
  --cond:'Roboto Condensed',sans-serif;
}
body{background:var(--bg);color:var(--white);font-family:'Inter',sans-serif;font-size:11px;height:100vh;display:flex;flex-direction:column;overflow:hidden}
/* TOPBAR */
#topbar{display:flex;align-items:center;justify-content:space-between;padding:0 12px;height:44px;background:var(--bg2);border-bottom:1px solid var(--bdr);flex-shrink:0}
#logo{font-family:var(--cond);font-size:18px;font-weight:900;letter-spacing:0.05em;display:flex;align-items:center;gap:8px}
#logo-dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
#logo-sub{font-size:8px;color:var(--muted);font-weight:400;letter-spacing:0.15em;display:block;margin-top:1px}
#conn-pill{display:flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--muted);border-radius:20px;cursor:pointer;color:var(--muted)}
#conn-pill .bd{width:6px;height:6px;border-radius:50%;background:var(--muted)}
#conn-txt{font-family:var(--cond);font-size:11px;font-weight:700;letter-spacing:0.1em}
#clock{font-family:var(--cond);font-size:13px;font-weight:700;color:var(--green);letter-spacing:0.05em}
/* PRICE STRIP */
#pricestrip{display:flex;align-items:center;gap:16px;padding:8px 12px;background:var(--bg2);border-bottom:1px solid var(--bdr);flex-shrink:0}
#spot-big{font-family:var(--cond);font-size:32px;font-weight:900;color:var(--white)}
#spot-big.up{color:var(--green)}#spot-big.dn{color:var(--red)}
#chg{font-size:10px;font-weight:600}
#chg.up{color:var(--green)}#chg.dn{color:var(--red)}
.ohlv{display:flex;flex-direction:column;gap:2px}
.ohlv-l{font-size:7px;color:var(--muted);letter-spacing:0.1em}
.ohlv-v{font-family:var(--cond);font-size:12px;font-weight:700}
#autolive-badge{margin-left:auto;font-size:8px;color:var(--muted);letter-spacing:0.1em}
/* GLOBALS BAR */
#globalsbar{display:flex;gap:12px;padding:4px 12px;background:var(--bg3);border-bottom:1px solid var(--bdr);flex-shrink:0}
.g-item{display:flex;gap:5px;align-items:center}
.g-l{font-size:7px;color:var(--muted);letter-spacing:0.1em}
.g-v{font-family:var(--cond);font-size:11px;font-weight:700}
.g-v.up{color:var(--green)}.g-v.dn{color:var(--red)}.g-v.neu{color:var(--yellow)}
/* SESSION BAR */
#sessionbar{padding:4px 12px;background:#1A1200;border-bottom:1px solid #3D2E00;font-size:9px;color:var(--yellow);letter-spacing:0.08em;font-weight:700;text-align:center;display:none;flex-shrink:0}
/* TABS */
#tabbar{display:flex;background:var(--bg2);border-bottom:1px solid var(--bdr);flex-shrink:0}
.tab{flex:1;padding:10px 4px;text-align:center;font-family:var(--cond);font-size:11px;font-weight:700;letter-spacing:0.08em;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;transition:all 0.15s}
.tab:hover{color:var(--white)}
.tab.on{color:var(--orange);border-bottom-color:var(--orange)}
/* PAGES */
#pages{flex:1;overflow:hidden;position:relative}
.page{display:none;height:100%;overflow-y:auto;padding:10px}
.page.on{display:block}
/* CARDS */
.card{background:var(--bg2);border:1px solid var(--bdr);border-radius:5px;margin-bottom:8px;overflow:hidden}
.card-hd{display:flex;justify-content:space-between;align-items:center;padding:6px 12px;font-family:var(--cond);font-size:9px;font-weight:700;letter-spacing:0.12em;color:var(--muted);border-bottom:1px solid var(--bdr);background:var(--bg3)}
.mi{background:var(--bg2);border:1px solid var(--bdr);border-radius:4px;padding:8px 10px}
.mi-l{font-size:7px;color:var(--muted);letter-spacing:0.1em;margin-bottom:3px}
.mi-v{font-family:var(--cond);font-size:18px;font-weight:900}
/* SIGNAL */
.sig-ac{border-radius:5px;padding:16px;margin-bottom:8px;text-align:center}
.sig-big{font-family:var(--cond);font-size:40px;font-weight:900;letter-spacing:0.05em}
.sig-sub{font-size:9px;font-weight:700;letter-spacing:0.15em;margin-top:4px}
/* CHART */
.chart-wrap{height:100%;display:flex;flex-direction:column}
.chart-hd{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:var(--bg2);border-bottom:1px solid var(--bdr)}
.chart-body{flex:1;position:relative;min-height:320px}
.tf{padding:3px 8px;background:var(--dim);border:1px solid var(--bdr);color:var(--muted);font-family:var(--cond);font-size:10px;font-weight:700;cursor:pointer;border-radius:3px}
.tf.on{background:var(--orange);color:#000;border-color:var(--orange)}
/* LOG BAR */
#logbar{padding:4px 12px;background:var(--bg3);border-top:1px solid var(--bdr);font-size:8px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0}
/* TOAST */
#toast{position:fixed;bottom:40px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--bg2);border:1px solid var(--bdr);border-radius:4px;padding:8px 16px;font-size:10px;font-weight:700;opacity:0;transition:all 0.3s;z-index:999;pointer-events:none}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
/* LOGIN OVERLAY */
#login-overlay{position:fixed;inset:0;background:rgba(6,10,16,0.96);z-index:500;display:none;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:30px}
#login-overlay.show{display:flex}
/* TRADES */
.trade-row{padding:8px 12px;border-bottom:1px solid var(--bdr)}
/* ARIA */
#aria-msgs{height:200px;overflow-y:auto;padding:8px}
.aria-msg{margin-bottom:8px;line-height:1.6}
.aria-msg.user{color:var(--teal);font-size:9px}
.aria-msg.bot{color:var(--white);font-size:10px}
#aria-input{width:100%;padding:8px 10px;background:var(--bg3);border:1px solid var(--bdr);color:var(--white);font-size:10px;outline:none}
</style>
</head>
<body>

<!-- TOPBAR -->
<div id="topbar">
  <div id="logo">
    <div id="logo-dot"></div>
    <div>
      <span style="display:block">BN TERMINAL</span>
      <span id="logo-sub">UPSTOX · LIVE</span>
    </div>
  </div>
  <div id="conn-pill">
    <div class="bd"></div>
    <span id="conn-txt">CLOSED</span>
  </div>
  <div id="clock">—:—:— IST</div>
</div>

<!-- PRICE STRIP -->
<div id="pricestrip">
  <div>
    <div id="spot-big">₹—</div>
    <div id="chg">—</div>
  </div>
  <div class="ohlv"><div class="ohlv-l">OPEN</div><div class="ohlv-v" id="d-o">—</div></div>
  <div class="ohlv"><div class="ohlv-l">HIGH</div><div class="ohlv-v" id="d-h" style="color:var(--green)">—</div></div>
  <div class="ohlv"><div class="ohlv-l">LOW</div><div class="ohlv-v" id="d-l" style="color:var(--red)">—</div></div>
  <div class="ohlv"><div class="ohlv-l">VWAP</div><div class="ohlv-v" id="d-vw">—</div></div>
  <div id="autolive-badge">AUTO-LIVE<br><span id="upd-ts">—</span></div>
</div>

<!-- GLOBALS BAR -->
<div id="globalsbar">
  <div class="g-item"><span class="g-l">VIX</span><span class="g-v" id="g-vix">—</span></div>
  <div class="g-item"><span class="g-l">PCR</span><span class="g-v" id="g-pcr">—</span></div>
  <div class="g-item"><span class="g-l">S&P</span><span class="g-v" id="g-sp">—</span></div>
  <div class="g-item"><span class="g-l">CRUDE</span><span class="g-v" id="g-cr">—</span></div>
  <div class="g-item"><span class="g-l">GOLD</span><span class="g-v" id="g-gd">—</span></div>
  <div class="g-item"><span class="g-l">₹/USD</span><span class="g-v" id="g-usd">—</span></div>
</div>

<!-- SESSION BAR -->
<div id="sessionbar">MARKET CLOSED · <span id="session-txt">Showing last session</span></div>

<!-- TABS -->
<div id="tabbar">
  <div class="tab on"  onclick="goTab(0)">📊 SIGNAL</div>
  <div class="tab"     onclick="goTab(1)">📈 CHART</div>
  <div class="tab"     onclick="goTab(2)">⛓ LEVELS</div>
  <div class="tab"     onclick="goTab(3)">🤖 ARIA</div>
  <div class="tab"     onclick="goTab(4)">📋 TRADES</div>
</div>

<!-- PAGES -->
<div id="pages">

  <!-- PAGE 0: SIGNAL -->
  <div class="page on" id="page-0">
    <div class="card">
      <div class="card-hd">SIGNAL ENGINE<span id="sig-ts">—</span></div>
      <div id="sig-body" style="padding:16px;text-align:center;color:var(--muted);font-size:10px">Waiting for data...</div>
    </div>
    <div class="card">
      <div class="card-hd">DAY BIAS</div>
      <div id="tl" style="padding:10px 12px;font-family:var(--cond);font-size:14px;font-weight:700;color:var(--muted)">—</div>
    </div>
    <div class="card">
      <div class="card-hd">LIVE INDICATORS</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--bdr)">
        <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">EMA 9</div><div class="mi-v" id="i-e9" style="font-size:14px">—</div><div id="i-e9b" style="height:3px;background:var(--green);width:30%;margin-top:4px"></div></div>
        <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">EMA 21</div><div class="mi-v" id="i-e21" style="font-size:14px">—</div></div>
        <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">RSI 14</div><div class="mi-v" id="i-rsi" style="font-size:14px">—</div><div id="i-rsib" style="height:3px;background:var(--yellow);width:50%;margin-top:4px"></div></div>
        <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">VWAP</div><div class="mi-v" id="i-vwap" style="font-size:14px;color:var(--teal)">—</div></div>
        <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">S.TREND</div><div class="mi-v" id="i-st" style="font-size:14px">—</div></div>
        <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">PCR</div><div class="mi-v" id="i-pcr" style="font-size:14px">—</div></div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div class="card">
        <div class="card-hd">PCR (OI)</div>
        <div style="padding:10px 12px"><div class="mi-v" id="m-pcr" style="font-size:20px">—</div><div id="m-pcr-l" style="font-size:8px;color:var(--muted);margin-top:2px">—</div></div>
      </div>
      <div class="card">
        <div class="card-hd">MAX PAIN</div>
        <div style="padding:10px 12px"><div class="mi-v" id="m-mp" style="font-size:20px;color:var(--yellow)">—</div><div style="font-size:8px;color:var(--muted);margin-top:2px">↑ strike</div></div>
      </div>
      <div class="card">
        <div class="card-hd">VIX</div>
        <div style="padding:10px 12px"><div class="mi-v" id="m-vix" style="font-size:20px">—</div><div id="m-vix-l" style="font-size:8px;color:var(--muted);margin-top:2px">—</div></div>
      </div>
      <div class="card">
        <div class="card-hd">TOTAL OI</div>
        <div style="padding:10px 12px"><div class="mi-v" id="m-toi" style="font-size:20px">—</div><div style="font-size:8px;color:var(--muted);margin-top:2px">CE+PE Lakh</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-hd">RISK CALCULATOR</div>
      <div style="padding:10px 12px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
        <div><div class="mi-l">Capital ₹</div><input id="rc-c" type="number" value="50000" style="width:100%;padding:4px;background:var(--bg3);border:1px solid var(--bdr);color:var(--white);font-size:11px"></div>
        <div><div class="mi-l">Entry ₹</div><input id="rc-e" type="number" placeholder="option premium" style="width:100%;padding:4px;background:var(--bg3);border:1px solid var(--bdr);color:var(--white);font-size:11px"></div>
        <div><div class="mi-l">Stop ₹</div><input id="rc-s" type="number" placeholder="sl premium" style="width:100%;padding:4px;background:var(--bg3);border:1px solid var(--bdr);color:var(--white);font-size:11px"></div>
      </div>
      <div id="rc-out" style="padding:0 12px 10px;font-size:10px;color:var(--teal)"></div>
    </div>
  </div>

  <!-- PAGE 1: CHART -->
  <div class="page" id="page-1">
    <div class="chart-wrap">
      <div class="chart-hd">
        <span style="font-family:var(--cond);font-size:11px;font-weight:700;color:var(--muted)">BANKNIFTY · UPSTOX LIVE</span>
        <button class="tf on" onclick="loadChart(1)">1m</button>
      </div>
      <div class="chart-body">
        <div id="lw_chart" style="width:100%;height:320px"></div>
        <div id="chart-loading" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-family:var(--cond);font-size:11px;color:var(--muted);font-weight:700;letter-spacing:0.1em">LOADING CHART...</div>
      </div>
      <div style="padding:6px 10px;display:flex;gap:12px;align-items:center;font-size:8px;color:var(--muted)">
        <span>— <span style="color:var(--green)">EMA 9</span></span>
        <span>— <span style="color:var(--orange)">EMA 21</span></span>
        <span>— <span style="color:var(--yellow)">VWAP</span></span>
      </div>
    </div>
  </div>

  <!-- PAGE 2: LEVELS -->
  <div class="page" id="page-2">
    <div class="card">
      <div class="card-hd">SUPPORT & RESISTANCE</div>
      <div id="sr-body" style="padding:10px 12px;color:var(--muted);font-size:10px">Calculating...</div>
    </div>
    <div class="card">
      <div class="card-hd">PIVOT LEVELS</div>
      <div id="piv-body" style="padding:10px 12px"></div>
    </div>
    <div class="card">
      <div class="card-hd">OPTION CHAIN SNAPSHOT</div>
      <div id="oc-body" style="padding:6px;overflow-x:auto"></div>
    </div>
  </div>

  <!-- PAGE 3: ARIA -->
  <div class="page" id="page-3">
    <div class="card">
      <div class="card-hd">ARIA · AI TRADING ANALYST</div>
      <div id="aria-msgs"></div>
      <div style="display:flex;border-top:1px solid var(--bdr)">
        <input id="aria-input" placeholder="Ask ARIA about market conditions..." onkeydown="if(event.key==='Enter')askAria()">
        <button onclick="askAria()" style="padding:8px 14px;background:var(--teal);color:#000;border:none;font-family:var(--cond);font-size:11px;font-weight:700;cursor:pointer">ASK</button>
      </div>
    </div>
  </div>

  <!-- PAGE 4: TRADES -->
  <div class="page" id="page-4">
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px">
      <div class="mi"><div class="mi-l">CAPITAL</div><div class="mi-v" style="font-size:16px">&#8377;1,00,000</div></div>
      <div class="mi"><div class="mi-l">AVAILABLE</div><div id="pt-avail" class="mi-v" style="font-size:16px;color:var(--teal)">—</div></div>
      <div class="mi"><div class="mi-l">TOTAL P&L</div><div id="pt-pnl" class="mi-v" style="font-size:16px">—</div></div>
      <div class="mi"><div class="mi-l">WIN RATE</div><div id="pt-wr" class="mi-v" style="font-size:16px">—</div></div>
    </div>
    <div class="card">
      <div class="card-hd">OPEN POSITION<span id="pt-open-time" style="font-weight:400;color:var(--muted)"></span></div>
      <div id="pt-open-body" style="padding:14px;text-align:center;color:var(--muted);font-size:10px">No open position — waiting for signal</div>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:8px">
      <button onclick="manualClose()" style="flex:1;padding:10px;background:var(--red);color:#fff;border:none;font-family:var(--cond);font-size:13px;font-weight:800;cursor:pointer;border-radius:4px">CLOSE TRADE</button>
      <button onclick="resetAccount()" style="padding:10px 14px;background:var(--dim);color:var(--muted);border:1px solid var(--bdr);font-family:var(--cond);font-size:11px;font-weight:800;cursor:pointer;border-radius:4px">RESET</button>
    </div>
    <div class="card">
      <div class="card-hd">TRADE HISTORY<span id="pt-count" style="font-weight:400;color:var(--muted)">0 trades</span></div>
      <div id="pt-history" style="max-height:280px;overflow-y:auto;padding:8px 0">
        <div style="text-align:center;color:var(--muted);font-size:10px;padding:16px">No trades yet</div>
      </div>
    </div>
    <div class="card">
      <div class="card-hd">&#129504; AI LEARNING LOG<span id="learn-days" style="font-weight:400;color:var(--muted)">0 days</span></div>
      <div id="learn-body" style="padding:10px 12px;text-align:center;color:var(--muted);font-size:10px">Learning data builds after market close each day</div>
    </div>
    <div style="padding:8px 12px;background:rgba(0,191,165,0.06);border:1px solid rgba(0,191,165,0.2);border-radius:4px;font-size:9px;color:var(--teal);line-height:1.8">
      Signal-based trading | Stop if -2% daily loss | Trade 9:20-15:00 IST | 5min gap | No fixed trade limit
    </div>
  </div>

</div>

<!-- LOG BAR -->
<div id="logbar"><span id="logtxt">Connecting...</span></div>

<!-- TOAST -->
<div id="toast"><span id="toast-msg"></span></div>

<!-- LOGIN OVERLAY -->
<div id="login-overlay">
  <div style="font-size:40px;margin-bottom:16px">&#128272;</div>
  <div style="font-family:var(--cond);font-size:28px;font-weight:900;color:var(--orange);margin-bottom:10px">SESSION EXPIRED</div>
  <div style="font-size:11px;color:var(--muted);margin-bottom:24px;line-height:1.8">Upstox token expired.<br>Login takes 10 seconds.</div>
  <a href="/login" style="padding:14px 32px;background:var(--orange);color:#000;font-weight:900;font-size:16px;border-radius:4px;text-decoration:none;letter-spacing:0.08em;font-family:var(--cond)">LOGIN WITH UPSTOX &#8594;</a>
  <div style="margin-top:16px;font-size:9px;color:var(--muted)">After login, come back to this page</div>
</div>

<script>
// ═══════════════ STATE ═══════════════
const S = {
  spot:0, open:0, high:0, low:0, vwap:0, change:0, pct:0,
  vix:0, pcr:0, sp500chg:0, crudechg:0, goldchg:0, usdinr:0,
  candles:[], signal:null, market_open:false
};
let prevSig = null;
let lastSignalFired = null;

// ═══════════════ UTILITIES ═══════════════
const f = n => Math.round(Math.abs(n||0)).toLocaleString('en-IN');
const fp = n => (n>=0?'+':'-') + '₹' + f(n);
const fc = n => n>=0 ? 'var(--green)' : 'var(--red)';
const cc = n => n>=0 ? 'up' : 'dn';

function toast(m) {
  const t = document.getElementById('toast');
  document.getElementById('toast-msg').textContent = m;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

function log(m, ok) {
  const el = document.getElementById('logtxt');
  el.textContent = m;
  el.style.color = ok === true ? 'var(--green)' : ok === false ? 'var(--red)' : 'var(--muted)';
}

// ═══════════════ CONNECTION ═══════════════
function setConn(ok, marketOpen, info) {
  const pill = document.getElementById('conn-pill');
  const txt = document.getElementById('conn-txt');
  const dot = pill.querySelector('.bd');
  const logo_dot = document.getElementById('logo-dot');
  let label, col;
  if (ok && marketOpen) { label = 'LIVE'; col = 'var(--green)'; }
  else if (ok && !marketOpen) { label = 'CLOSED'; col = 'var(--yellow)'; }
  else { label = 'OFFLINE'; col = 'var(--red)'; }
  txt.textContent = label;
  pill.style.color = col;
  pill.style.borderColor = col;
  dot.style.background = col;
  logo_dot.style.background = col;
  S.market_open = marketOpen;
  const sb = document.getElementById('sessionbar');
  if (ok && !marketOpen && info) {
    sb.style.display = 'block';
    document.getElementById('session-txt').textContent = 'Showing last session: ' + info;
  } else {
    sb.style.display = 'none';
  }
}

function showLoginOverlay() {
  document.getElementById('login-overlay').classList.add('show');
}

function hideLoginOverlay() {
  document.getElementById('login-overlay').classList.remove('show');
}

// ═══════════════ CLOCK ═══════════════
setInterval(() => {
  const now = new Date(Date.now() + 5.5*3600000);
  document.getElementById('clock').textContent =
    String(now.getUTCHours()).padStart(2,'0') + ':' +
    String(now.getUTCMinutes()).padStart(2,'0') + ':' +
    String(now.getUTCSeconds()).padStart(2,'0') + ' IST';
}, 1000);

// ═══════════════ TAB NAVIGATION ═══════════════
function goTab(i) {
  document.querySelectorAll('.tab').forEach((t,j) => t.classList.toggle('on', i===j));
  document.querySelectorAll('.page').forEach((p,j) => p.classList.toggle('on', i===j));
  if (i === 1) setTimeout(() => { if (!lwC) initChart(); else loadChart(1); }, 100);
  if (i === 4) fetchTrades();
}

// ═══════════════ PRICE FETCH ═══════════════
async function fetchFromServer() {
  try {
    const res = await fetch('/api/price', {cache:'no-store'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const d = await res.json();

    if (!d.authenticated) {
      setConn(false, false, '');
      showLoginOverlay();
      return;
    }

    hideLoginOverlay();

    const spot = d.spot || 0;
    if (spot < 30000) {
      setConn(true, false, d.last_session_time || '');
      log('Market closed · Last: ' + (d.last_session_time || '—'), null);
      return;
    }

    S.spot = spot;
    S.change = d.change || 0;
    S.pct = d.pct || 0;
    S.high = d.high || spot;
    S.low = d.low || spot;
    S.open = d.open || spot;
    S.vwap = d.vwap || (d.high + d.low + spot) / 3;
    S.vix = d.vix || 0;
    S.pcr = d.pcr || 0;
    S.sp500chg = d.sp500_chg || 0;
    S.crudechg = d.crude_chg || 0;
    S.goldchg = d.gold_chg || 0;
    S.usdinr = d.usdinr || 0;

    // Update candles
    const now = new Date(Date.now() + 5.5*3600000);
    const t = String(now.getUTCHours()).padStart(2,'0') + ':' + String(now.getUTCMinutes()).padStart(2,'0');
    const last = S.candles[S.candles.length-1];
    if (last && last.t === t) {
      last.c = spot; last.h = Math.max(last.h, spot); last.l = Math.min(last.l, spot);
    } else {
      S.candles.push({t, o: S.spot||spot, h: spot, l: spot, c: spot, v: 1});
    }
    if (S.candles.length > 300) S.candles = S.candles.slice(-300);

    document.getElementById('upd-ts').textContent = d.last_updated || '—';
    setConn(true, d.market_open !== false, d.last_session_time || '');

    const using_last = d.using_last_session;
    log((using_last ? 'Last session · ' : 'Live · ') + 'BN ₹' + f(spot) + ' · VIX ' + (S.vix||'—'), using_last ? null : true);

    updatePriceDisplay();
    renderAll();
    updateChartTick(spot, S.high, S.low);

  } catch(e) {
    setConn(false, false, '');
    log('Error: ' + e.message, false);
  }
}

function updatePriceDisplay() {
  const el = document.getElementById('spot-big');
  el.textContent = '₹' + f(S.spot);
  el.className = cc(S.change);

  const chg = document.getElementById('chg');
  chg.textContent = (S.change>=0?'▲':'▼') + Math.abs(S.change).toFixed(2) + ' (' + Math.abs(S.pct).toFixed(2) + '%)';
  chg.className = cc(S.change);

  document.getElementById('d-o').textContent = f(S.open);
  document.getElementById('d-h').textContent = f(S.high);
  document.getElementById('d-l').textContent = f(S.low);
  document.getElementById('d-vw').textContent = f(S.vwap);

  const vix = document.getElementById('g-vix');
  vix.textContent = S.vix ? S.vix.toFixed(1) : '—';
  vix.className = 'g-v ' + (S.vix>20?'dn':S.vix>15?'neu':'up');

  const pcr = document.getElementById('g-pcr');
  pcr.textContent = S.pcr ? S.pcr.toFixed(2) : '—';
  pcr.className = 'g-v ' + (S.pcr>=1.2?'up':S.pcr>=0.9?'neu':'dn');

  [['g-sp',S.sp500chg],['g-cr',S.crudechg],['g-gd',S.goldchg]].forEach(([id,v]) => {
    const el = document.getElementById(id);
    el.textContent = v ? (v>=0?'+':'')+v.toFixed(2)+'%' : '—';
    el.className = 'g-v ' + cc(v);
  });
  document.getElementById('g-usd').textContent = S.usdinr ? S.usdinr.toFixed(1) : '—';
}

// ═══════════════ MATH HELPERS ═══════════════
function ema(data, period) {
  if (data.length < period) return data[data.length-1] || 0;
  const k = 2/(period+1);
  let e = data.slice(0,period).reduce((a,b)=>a+b,0)/period;
  for (let i = period; i < data.length; i++) e = data[i]*k + e*(1-k);
  return e;
}

function rsi(data, period) {
  if (data.length < period+1) return 50;
  let gains=0, losses=0;
  for (let i=data.length-period; i<data.length; i++) {
    const d = data[i]-data[i-1];
    if (d>0) gains+=d; else losses-=d;
  }
  const rs = gains/Math.max(losses,0.001);
  return 100 - 100/(1+rs);
}

function vwapCalc(candles) {
  if (!candles.length) return 0;
  let tv=0, tp=0;
  candles.forEach(c => { const v=c.v||1; tp+=((c.h+c.l+c.c)/3)*v; tv+=v; });
  return tp/tv;
}

function supertrend(candles, period=7, mult=3) {
  if (candles.length < period+1) return {bull:true, val:0};
  const atrs = [];
  for (let i=1; i<candles.length; i++) {
    atrs.push(Math.max(candles[i].h-candles[i].l, Math.abs(candles[i].h-candles[i-1].c), Math.abs(candles[i].l-candles[i-1].c)));
  }
  const atr = atrs.slice(-period).reduce((a,b)=>a+b,0)/period;
  const last = candles[candles.length-1];
  const mid = (last.h+last.l)/2;
  const bull = last.c > mid - mult*atr;
  return {bull, val: bull ? mid - mult*atr : mid + mult*atr};
}

function calcPivots(candles) {
  if (!candles.length) return {P:0,R1:0,R2:0,R3:0,S1:0,S2:0,S3:0};
  const yest = candles.length > 60 ? candles.slice(-75,-1) : candles;
  const H = Math.max(...yest.map(c=>c.h));
  const L = Math.min(...yest.map(c=>c.l));
  const C = yest[yest.length-1].c;
  const P = (H+L+C)/3;
  return {
    P, R1:2*P-L, R2:P+(H-L), R3:H+2*(P-L),
    S1:2*P-H, S2:P-(H-L), S3:L-2*(H-P)
  };
}

// ═══════════════ MARKET BEHAVIOUR ═══════════════
function readMarketBehaviour(cs, spot, vix) {
  if (cs.length < 5) return {behaviour:'UNKNOWN', strength:0, action:'WAIT', reason:'Need more data'};
  const cl = cs.map(c=>c.c);
  const e9 = ema(cl,9), e21 = ema(cl,21);
  const rs = rsi(cl,14);
  const vw = vwapCalc(cs);
  const ptMove = cl.length>=10 ? cl[cl.length-1]-cl[cl.length-10] : cl[cl.length-1]-cl[0];
  const last5 = cs.slice(-5);
  const bullCandles = last5.filter(c=>c.c>c.o).length;
  const bearCandles = last5.filter(c=>c.c<c.o).length;
  const strongBull = last5.filter(c=>c.c>c.o&&(c.c-c.o)>(c.h-c.l)*0.4).length;
  const strongBear = last5.filter(c=>c.c<c.o&&(c.o-c.c)>(c.h-c.l)*0.4).length;
  const recentVol = cs.slice(-5).reduce((a,c)=>a+(c.v||1),0)/5;
  const prevVol = cs.slice(-10,-5).reduce((a,c)=>a+(c.v||1),0)/5;
  const volRising = recentVol > prevVol*1.1;
  const emaBull = e9>e21, emaBear = e9<e21;
  const aboveVwap = spot>vw, belowVwap = spot<vw;
  let behaviour, strength, action, reason;
  if (emaBull && aboveVwap && bullCandles>=3 && ptMove>50) {
    behaviour='STRONG_UPTREND'; action='BUY_CALL';
    strength=Math.min(95,55+strongBull*8+(volRising?10:0)+(ptMove>100?10:0));
    reason='+'+Math.round(ptMove)+'pts | '+bullCandles+'/5 bull | EMA bull | Above VWAP';
  } else if (emaBear && belowVwap && bearCandles>=3 && ptMove<-50) {
    behaviour='STRONG_DOWNTREND'; action='BUY_PUT';
    strength=Math.min(95,55+strongBear*8+(volRising?10:0)+(ptMove<-100?10:0));
    reason=Math.round(ptMove)+'pts | '+bearCandles+'/5 bear | EMA bear | Below VWAP';
  } else if (emaBull && aboveVwap && bullCandles>=2) {
    behaviour='MILD_UPTREND'; action='WATCH_LONG';
    strength=48+strongBull*5+(volRising?8:0);
    reason=bullCandles+'/5 bull | EMA bull | Above VWAP';
  } else if (emaBear && belowVwap && bearCandles>=2) {
    behaviour='MILD_DOWNTREND'; action='WATCH_SHORT';
    strength=48+strongBear*5+(volRising?8:0);
    reason=bearCandles+'/5 bear | EMA bear | Below VWAP';
  } else if (emaBull && bullCandles>=3) {
    behaviour='MILD_UPTREND'; action='WATCH_LONG';
    strength=42+strongBull*5;
    reason=bullCandles+'/5 bull | EMA bull (near VWAP)';
  } else if (emaBear && bearCandles>=3) {
    behaviour='MILD_DOWNTREND'; action='WATCH_SHORT';
    strength=42+strongBear*5;
    reason=bearCandles+'/5 bear | EMA bear (near VWAP)';
  } else if (rs>72 && e9<e21) {
    behaviour='POSSIBLE_REVERSAL_DOWN'; action='WATCH_SHORT'; strength=52;
    reason='RSI overbought '+Math.round(rs)+' | EMA turning';
  } else if (rs<28 && e9>e21) {
    behaviour='POSSIBLE_REVERSAL_UP'; action='WATCH_LONG'; strength=52;
    reason='RSI oversold '+Math.round(rs)+' | EMA turning';
  } else {
    behaviour='RANGING'; action='WAIT'; strength=15;
    reason='No clear direction | bull:'+bullCandles+' bear:'+bearCandles;
  }
  return {behaviour, strength, action, reason, rs, e9, e21, vw, ptMove};
}

// ═══════════════ SIGNAL ENGINE v3 ═══════════════
function computeTrend(cs, spot) {
  if (cs.length<3) return null;
  const cl = cs.map(c=>c.c);
  const e9=ema(cl,9), e21=ema(cl,21), rs=rsi(cl,14), vw=vwapCalc(cs), st=supertrend(cs);
  const slope = (cl[cl.length-1]-cl[Math.max(0,cl.length-6)])/6;
  const bulls = [spot>vw, e9>e21, st.bull, rs>52, slope>5, S.sp500chg>0, S.vix<18||S.vix===0, S.open>0&&spot>S.open];
  const bears = [spot<vw, e9<e21, !st.bull, rs<48, slope<-5, S.sp500chg<0, S.vix>18, S.open>0&&spot<S.open];
  const bs=bulls.filter(Boolean).length, br=bears.filter(Boolean).length, net=bs-br;
  let label,col,strat;
  if(net>=5){label='STRONGLY BULLISH';col='var(--green)';strat='Strong uptrend';}
  else if(net>=3){label='BULLISH';col='#66BB6A';strat='Bullish — buy pullbacks';}
  else if(net>=1){label='MILDLY BULLISH';col='var(--teal)';strat='Cautious CE only';}
  else if(net>=-1){label='SIDEWAYS';col='var(--yellow)';strat='Wait for breakout';}
  else if(net>=-3){label='MILDLY BEARISH';col='#FFA040';strat='Cautious PE only';}
  else if(net>=-5){label='BEARISH';col='#FF7043';strat='Sell rallies';}
  else{label='STRONGLY BEARISH';col='var(--red)';strat='Strong downtrend';}
  return {label,col,strat,bs,br,meta:{e9:+e9.toFixed(0),e21:+e21.toFixed(0),rsi:rs,vwap:+vw.toFixed(0),st}};
}

function computeSignal(cs, spot) {
  const piv = calcPivots(cs);
  const atm = Math.round(spot/100)*100;
  const base = {signal:'WAIT',conf:0,gate:'',strike:atm,otype:'-',sl:null,t1:piv.R1,t2:piv.R2,t3:piv.R3,pivots:piv,meta:{e9:0,e21:0,rsi:50,vwap:S.vwap,st:{bull:true}}};
  if (cs.length<5) { base.gate='Building data... ('+cs.length+'/5 candles)'; return base; }
  const cl = cs.map(c=>c.c);
  const e9=ema(cl,9), e21=ema(cl,21), rs=rsi(cl,14), vw=vwapCalc(cs), st=supertrend(cs);
  const vix=S.vix||0;
  const beh=readMarketBehaviour(cs,spot,vix);
  const ist=new Date(Date.now()+5.5*3600000);
  const hh=ist.getUTCHours(), mm=ist.getUTCMinutes();
  const cur=hh*60+mm;
  if(cur<9*60+20){base.gate='Pre-market — waiting for 9:20 AM';return base;}
  if(cur>15*60){base.gate='Market closing — no new trades';return base;}
  const meta={e9:+e9.toFixed(0),e21:+e21.toFixed(0),rsi:rs,vwap:+vw.toFixed(0),st};

  if(beh.action==='BUY_CALL'&&beh.strength>=50){
    return{...base,signal:'BUY',conf:Math.min(92,beh.strength),gate:null,otype:'CE',
      sl:spot-120,t1:piv.R1,t2:piv.R2,t3:piv.R3,meta,behaviour:beh.behaviour,regime_note:beh.reason};
  }
  if(beh.action==='BUY_PUT'&&beh.strength>=50){
    return{...base,signal:'SELL',conf:Math.min(92,beh.strength),gate:null,otype:'PE',
      sl:spot+120,t1:piv.S1,t2:piv.S2,t3:piv.S3,meta,behaviour:beh.behaviour,regime_note:beh.reason};
  }
  if(beh.action==='WATCH_LONG'&&beh.strength>=45&&e9>e21&&spot>vw){
    return{...base,signal:'BUY',conf:Math.min(78,beh.strength+10),gate:null,otype:'CE',
      sl:spot-120,t1:piv.R1,t2:piv.R2,t3:piv.R3,meta,behaviour:'TREND_CONTINUATION',regime_note:beh.reason};
  }
  if(beh.action==='WATCH_SHORT'&&beh.strength>=45&&e9<e21&&spot<vw){
    return{...base,signal:'SELL',conf:Math.min(78,beh.strength+10),gate:null,otype:'PE',
      sl:spot+120,t1:piv.S1,t2:piv.S2,t3:piv.S3,meta,behaviour:'TREND_CONTINUATION',regime_note:beh.reason};
  }

  // Standard 8-factor
  const bull={'Price>VWAP':spot>vw,'EMA9>EMA21':e9>e21,'Supertrend Bull':st.bull,'RSI 45-65':rs>=45&&rs<=65,'Momentum Up':beh.ptMove>30,'PCR>0.9':S.pcr>0.9,'RSI>50':rs>50,'S&P+':S.sp500chg>0};
  const bear={'Price<VWAP':spot<vw,'EMA9<EMA21':e9<e21,'Supertrend Bear':!st.bull,'RSI 35-55':rs>=35&&rs<=55,'Momentum Down':beh.ptMove<-30,'PCR<1.0':S.pcr<1.0,'RSI<50':rs<50,'S&P-':S.sp500chg<0};
  const bs=Object.values(bull).filter(Boolean).length;
  const br=Object.values(bear).filter(Boolean).length;
  const thr=vix>20?5:4;
  let sig='WAIT', conds=bs>=br?bull:bear;
  if(bs>=thr){sig='BUY';conds=bull;}
  else if(br>=thr){sig='SELL';conds=bear;}
  const score=sig==='BUY'?bs:sig==='SELL'?br:Math.max(bs,br);
  const conf=sig==='WAIT'?Math.round(score/8*100):Math.min(88,58+score*5);
  if(conf<55&&sig!=='WAIT'){base.gate='Conf '+conf+'% too low';return base;}
  return{signal:sig,conf,gate:null,strike:atm,
    otype:sig==='BUY'?'CE':sig==='SELL'?'PE':'-',
    sl:sig==='BUY'?spot-120:sig==='SELL'?spot+120:null,
    t1:sig==='BUY'?piv.R1:piv.S1,t2:sig==='BUY'?piv.R2:piv.S2,t3:sig==='BUY'?piv.R3:piv.S3,
    pivots:piv,cl:conds,bs,br,meta,behaviour:beh.behaviour,regime_note:beh.reason};
}

// ═══════════════ RENDER ═══════════════
function renderAll() {
  if (!S.spot) return;
  const t = computeTrend(S.candles, S.spot);
  renderTrend(t);
  const sig = computeSignal(S.candles, S.spot);
  S.signal = sig;
  renderSignal(sig);
  renderInds(sig.meta||{});
  renderMinis();
  checkPaperTrade(sig);
  updateChartTick(S.spot, S.high, S.low);
  // TradeBrain
  if (TradeBrain.active && S.candles.length>=3) {
    const cl2=S.candles.map(c=>c.c);
    const result=TradeBrain.tick(S.spot,S.candles,ema(cl2,9),ema(cl2,21),vwapCalc(S.candles),rsi(cl2,14));
    if (result && result.action==='EXIT') {
      fetch('/api/trades/close_reason',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:result.reason,spot:S.spot})}).then(()=>fetchTrades());
      TradeBrain.reset();
      toast('Trade exited: '+result.reason);
    }
  }
  if (prevSig && prevSig!=='WAIT' && sig.signal!==prevSig && sig.signal!=='WAIT') {
    toast('Signal: '+prevSig+' → '+sig.signal);
  }
  prevSig = sig.signal;
}

function renderTrend(t) {
  const el = document.getElementById('tl');
  if (!t) { el.textContent = S.candles.length<5?'Collecting...':'—'; el.style.color='var(--muted)'; return; }
  el.textContent = t.label + ' · ' + t.strat;
  el.style.color = t.col;
}

function renderSignal(sig) {
  const body = document.getElementById('sig-body');
  const ts = document.getElementById('sig-ts');
  const ist = new Date(Date.now()+5.5*3600000);
  ts.textContent = String(ist.getUTCHours()).padStart(2,'0')+':'+String(ist.getUTCMinutes()).padStart(2,'0')+' IST';
  if (!sig || sig.signal==='WAIT') {
    const msg = sig && sig.gate ? sig.gate : 'Analyzing market...';
    body.innerHTML = '<div style="padding:20px;text-align:center"><div style="font-size:32px;margin-bottom:8px">⏳</div><div style="font-family:var(--cond);font-size:16px;color:var(--muted)">WAIT</div><div style="font-size:9px;color:var(--muted);margin-top:6px">' + msg + '</div></div>';
    return;
  }
  const isBuy = sig.otype==='CE';
  const col = isBuy ? 'var(--green)' : 'var(--red)';
  const bg = isBuy ? 'rgba(0,230,118,0.08)' : 'rgba(255,23,68,0.08)';
  const piv = sig.pivots || {};
  const conf = sig.conf || 65;
  body.innerHTML = `
    <div class="sig-ac" style="background:${bg}">
      <div class="sig-big" style="color:${col}">${isBuy?'BUY CALL':'BUY PUT'}</div>
      <div class="sig-sub" style="color:${col}">${sig.behaviour||'SIGNAL'}</div>
      ${sig.regime_note?`<div style="font-size:8px;color:var(--muted);margin-top:4px">${sig.regime_note}</div>`:''}
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--bdr);margin-bottom:8px">
      <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">STRIKE</div><div style="font-family:var(--cond);font-size:18px;font-weight:900">${(sig.strike||0).toLocaleString('en-IN')} ${sig.otype}</div></div>
      <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">CONFIDENCE</div><div style="font-family:var(--cond);font-size:18px;font-weight:900;color:${col}">${conf}%</div></div>
      <div style="background:var(--bg2);padding:8px 10px"><div class="mi-l">STOP LOSS</div><div style="font-family:var(--cond);font-size:18px;font-weight:900;color:var(--red)">${Math.round(sig.sl||0).toLocaleString('en-IN')}</div></div>
    </div>
    <div style="display:flex;gap:1px;background:var(--bdr)">
      <div style="flex:1;background:var(--bg2);padding:8px 10px;border-left:3px solid var(--teal)"><div class="mi-l">TARGET 1</div><div style="font-family:var(--cond);font-size:14px;font-weight:900;color:var(--teal)">${Math.round(sig.t1||0).toLocaleString('en-IN')}</div></div>
      <div style="flex:1;background:var(--bg2);padding:8px 10px;border-left:3px solid var(--green)"><div class="mi-l">TARGET 2</div><div style="font-family:var(--cond);font-size:14px;font-weight:900;color:var(--green)">${Math.round(sig.t2||0).toLocaleString('en-IN')}</div></div>
      <div style="flex:1;background:var(--bg2);padding:8px 10px;border-left:3px solid #888"><div class="mi-l">TARGET 3</div><div style="font-family:var(--cond);font-size:14px;font-weight:900;color:var(--muted)">${Math.round(sig.t3||0).toLocaleString('en-IN')}</div></div>
    </div>
    <div style="padding:8px 12px;font-size:8px;color:var(--muted)">Bull: ${sig.bs||0}/8 · Bear: ${sig.br||0}/8 · Spot: ₹${f(S.spot)} · VWAP: ₹${f(sig.meta&&sig.meta.vwap||0)}</div>`;
}

function renderInds(meta) {
  if (!meta) return;
  const e9 = meta.e9||0, e21 = meta.e21||0, rs = meta.rsi||50;
  document.getElementById('i-e9').textContent = e9 ? e9.toLocaleString('en-IN') : '—';
  document.getElementById('i-e9').style.color = e9>e21 ? 'var(--green)' : 'var(--red)';
  document.getElementById('i-e21').textContent = e21 ? e21.toLocaleString('en-IN') : '—';
  document.getElementById('i-rsi').textContent = rs ? Math.round(rs) : '—';
  document.getElementById('i-rsi').style.color = rs>70?'var(--red)':rs<30?'var(--green)':'var(--yellow)';
  document.getElementById('i-rsib').style.width = (rs||50)+'%';
  document.getElementById('i-rsib').style.background = rs>70?'var(--red)':rs<30?'var(--green)':'var(--yellow)';
  document.getElementById('i-vwap').textContent = meta.vwap ? f(meta.vwap) : '—';
  const st = meta.st;
  const stEl = document.getElementById('i-st');
  stEl.textContent = st ? (st.bull?'BULL ▲':'BEAR ▼') : '—';
  stEl.style.color = st ? (st.bull?'var(--green)':'var(--red)') : 'var(--muted)';
  document.getElementById('i-pcr').textContent = S.pcr ? S.pcr.toFixed(2) : '—';
  document.getElementById('i-e9b').style.width = Math.min(100, e9/(e21||1)*50)+'%';
}

function renderMinis() {
  const vix = S.vix||0;
  const vixEl = document.getElementById('m-vix');
  vixEl.textContent = vix ? vix.toFixed(1) : '—';
  vixEl.style.color = vix>25?'var(--red)':vix>18?'var(--yellow)':'var(--green)';
  document.getElementById('m-vix-l').textContent = vix>25?'DANGER':vix>18?'ELEVATED':'NORMAL';
  const pcr = S.pcr||0;
  const pcrEl = document.getElementById('m-pcr');
  pcrEl.textContent = pcr ? pcr.toFixed(2) : '—';
  pcrEl.style.color = pcr>=1.2?'var(--green)':pcr>=0.9?'var(--yellow)':'var(--red)';
  document.getElementById('m-pcr-l').textContent = pcr>=1.2?'Bullish':pcr>=0.9?'Neutral':'Bearish';
}

// ═══════════════ SUPPORT & RESISTANCE ═══════════════
function renderSR(piv, spot) {
  if (!piv || !spot) return;
  const f2 = n => Math.round(n).toLocaleString('en-IN');
  const levels = [
    {l:'R3',v:piv.R3,c:'var(--red)'},{l:'R2',v:piv.R2,c:'var(--orange)'},{l:'R1',v:piv.R1,c:'var(--yellow)'},
    {l:'PP',v:piv.P,c:'var(--white)'},{l:'S1',v:piv.S1,c:'var(--teal)'},{l:'S2',v:piv.S2,c:'var(--blue)'},{l:'S3',v:piv.S3,c:'#7C4DFF'}
  ];
  const srBody = document.getElementById('sr-body');
  const pivBody = document.getElementById('piv-body');
  if (srBody) srBody.innerHTML = levels.map(l => {
    const dist = Math.round(l.v - spot);
    const near = Math.abs(dist) < 50;
    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--bdr);${near?'background:rgba(255,255,255,0.03)':''}">
      <span style="font-family:var(--cond);font-weight:700;color:${l.c}">${l.l}</span>
      <span style="font-family:var(--cond);font-size:13px;font-weight:900">₹${f2(l.v)}</span>
      <span style="font-size:9px;color:${dist>0?'var(--green)':'var(--red)'}">${dist>0?'+':''}${dist}pts</span>
    </div>`;
  }).join('');
}

// ═══════════════ CHART ═══════════════
let lwC=null, cSeries=null, e9S=null, e21S=null, vwS=null;

function initChart() {
  const el = document.getElementById('lw_chart');
  if (!el || lwC) return;
  const loading = document.getElementById('chart-loading');
  lwC = LightweightCharts.createChart(el, {
    width: el.clientWidth || window.innerWidth,
    height: 320,
    layout: {background:{color:'#060A10'}, textColor:'#4A6080'},
    grid: {vertLines:{color:'#1E2D45'}, horzLines:{color:'#1E2D45'}},
    crosshair: {mode:1},
    rightPriceScale: {borderColor:'#1E2D45'},
    timeScale: {borderColor:'#1E2D45', timeVisible:true},
    handleScroll: true,
    handleScale: true,
  });
  cSeries = lwC.addCandlestickSeries({upColor:'#00E676',downColor:'#FF1744',borderVisible:false,wickUpColor:'#00E676',wickDownColor:'#FF1744'});
  e9S = lwC.addLineSeries({color:'#00BFA5',lineWidth:1,priceLineVisible:false});
  e21S = lwC.addLineSeries({color:'#FF6D00',lineWidth:1,priceLineVisible:false});
  vwS = lwC.addLineSeries({color:'#FFD600',lineWidth:1,lineStyle:2,priceLineVisible:false});
  if (loading) loading.style.display='none';
  loadChart(1);
}

async function loadChart(iv) {
  try {
    const res = await fetch('/api/candles?interval='+iv);
    const d = await res.json();
    if (!d.candles || !d.candles.length) return;
    const IST = 19800;
    const c = d.candles.map(x=>({time:x.time+IST,open:x.open,high:x.high,low:x.low,close:x.close}));
    cSeries.setData(c);
    const closes = c.map(x=>x.close);
    e9S.setData(c.map((x,i)=>({time:x.time,value:ema(closes.slice(0,i+1),9)})));
    e21S.setData(c.map((x,i)=>({time:x.time,value:ema(closes.slice(0,i+1),21)})));
    // VWAP
    let tv=0,tp=0;
    vwS.setData(d.candles.map((x,i)=>{const v=x.volume||1;tp+=((x.high+x.low+x.close)/3)*v;tv+=v;return{time:x.time+IST,value:tp/tv};}));
    lwC.timeScale().fitContent();
    window._chartCandles = c.slice();
    window._lastCandle = c[c.length-1];
  } catch(e) { console.log('Chart error:',e); }
}

function updateChartTick(spot, high, low) {
  if (!cSeries || !lwC || !spot) return;
  try {
    const IST = 19800;
    const now = Math.floor(Date.now()/1000) + IST;
    const minuteTs = now - (now%60);
    const last = window._lastCandle;
    if (!last) return;
    const updated = {
      time: minuteTs,
      open: last.time===minuteTs ? last.open : spot,
      high: last.time===minuteTs ? Math.max(last.high, high||spot) : spot,
      low:  last.time===minuteTs ? Math.min(last.low,  low||spot)  : spot,
      close: spot
    };
    window._lastCandle = updated;
    cSeries.update(updated);
  } catch(e) {}
}

// ═══════════════ LEVELS PAGE ═══════════════
function updateLevels() {
  if (!S.spot) return;
  const piv = calcPivots(S.candles);
  renderSR(piv, S.spot);
}
setInterval(updateLevels, 10000);

// ═══════════════ TRADE BRAIN v1 ═══════════════
const TradeBrain = {
  active:false, direction:null, entry_spot:0, trail_sl:0, highest_profit:0, candles_in_trade:0,
  config:{trail_start_pts:100, trail_distance:60, max_candles:75},
  reset() { this.active=false; this.direction=null; this.entry_spot=0; this.trail_sl=0; this.highest_profit=0; this.candles_in_trade=0; },
  start(dir, entry, sl) { this.active=true; this.direction=dir; this.entry_spot=entry; this.trail_sl=sl; this.highest_profit=0; this.candles_in_trade=0; },
  tick(spot, cs, e9, e21, vwap, rs) {
    if (!this.active) return null;
    this.candles_in_trade++;
    const pts = this.direction==='LONG' ? spot-this.entry_spot : this.entry_spot-spot;
    if (pts > this.highest_profit) this.highest_profit = pts;
    // Max time
    if (this.candles_in_trade >= this.config.max_candles) return this._exit('TIME LIMIT - 75min', spot);
    // Trail stop
    if (this.highest_profit >= this.config.trail_start_pts) {
      const new_sl = this.direction==='LONG' ? spot-this.config.trail_distance : spot+this.config.trail_distance;
      if (this.direction==='LONG' && new_sl>this.trail_sl) this.trail_sl=new_sl;
      if (this.direction==='SHORT' && new_sl<this.trail_sl) this.trail_sl=new_sl;
    }
    // SL hit
    const sl_hit = this.direction==='LONG' ? spot<=this.trail_sl : spot>=this.trail_sl;
    if (sl_hit) return this._exit(this.highest_profit>=this.config.trail_start_pts?'TRAIL STOP HIT':'STOP LOSS HIT', spot);
    // EMA flip
    if (this.candles_in_trade>5) {
      if (this.direction==='LONG' && e9<e21 && spot<vwap && pts<0) return this._exit('REVERSAL DETECTED', spot);
      if (this.direction==='SHORT' && e9>e21 && spot>vwap && pts<0) return this._exit('REVERSAL DETECTED', spot);
    }
    // 3 reversal candles
    if (cs.length>=3) {
      const last3=cs.slice(-3);
      if (this.direction==='LONG' && last3.every(c=>c.c<c.o) && pts<-30) return this._exit('3 REVERSAL CANDLES', spot);
      if (this.direction==='SHORT' && last3.every(c=>c.c>c.o) && pts<-30) return this._exit('3 REVERSAL CANDLES', spot);
    }
    // RSI extreme
    if (this.direction==='LONG' && rs>75 && pts>50) return this._exit('RSI OVERBOUGHT - TAKING PROFIT', spot);
    if (this.direction==='SHORT' && rs<25 && pts>50) return this._exit('RSI OVERSOLD - TAKING PROFIT', spot);
    return {action:'HOLD', pts, trail_sl:this.trail_sl};
  },
  _exit(reason, spot) {
    const pts = this.direction==='LONG' ? spot-this.entry_spot : this.entry_spot-spot;
    this.active = false;
    return {action:'EXIT', reason, spot, pts};
  }
};

// ═══════════════ PAPER TRADING ═══════════════
async function fetchTrades() {
  try {
    const res = await fetch('/api/trades');
    const d = await res.json();
    renderTrades(d);
  } catch(e) {}
  try {
    const lr = await fetch('/api/learning');
    const ld = await lr.json();
    renderLearning(ld);
  } catch(e) {}
}

function renderTrades(d) {
  if (!d) return;
  const availEl=document.getElementById('pt-avail');
  const pnlEl=document.getElementById('pt-pnl');
  const wrEl=document.getElementById('pt-wr');
  const cntEl=document.getElementById('pt-count');
  if (availEl) availEl.textContent = '₹'+f(d.available||0);
  if (pnlEl) { pnlEl.textContent=fp(d.stats&&d.stats.pnl||0); pnlEl.style.color=fc(d.stats&&d.stats.pnl||0); }
  const wr=d.stats&&d.stats.total>0?Math.round(d.stats.wins/d.stats.total*100):0;
  if (wrEl) { wrEl.textContent=wr+'%'; wrEl.style.color=wr>=55?'var(--green)':wr>=40?'var(--yellow)':'var(--red)'; }
  if (cntEl) cntEl.textContent=(d.stats&&d.stats.total||0)+' trades';
  const body=document.getElementById('pt-open-body');
  const timeEl=document.getElementById('pt-open-time');
  if (body) {
    if (d.open_trade) {
      const t=d.open_trade;
      const isBuy=t.otype==='CE';
      const col=isBuy?'var(--green)':'var(--red)';
      const lp=d.live_pnl||0;
      if(timeEl) timeEl.textContent=' | '+t.time;
      body.innerHTML='<div style="display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--bdr)">'
        +'<div style="background:var(--bg2);padding:10px 12px"><div class="mi-l">SIGNAL</div><div style="font-family:var(--cond);font-size:22px;font-weight:900;color:'+col+'">'+(isBuy?'BUY CALL':'BUY PUT')+'</div></div>'
        +'<div style="background:var(--bg2);padding:10px 12px"><div class="mi-l">STRIKE</div><div style="font-family:var(--cond);font-size:22px;font-weight:900">'+(t.strike||0).toLocaleString('en-IN')+' '+t.otype+'</div></div>'
        +'<div style="background:var(--bg2);padding:10px 12px"><div class="mi-l">ENTRY PREMIUM</div><div style="font-family:var(--cond);font-size:18px;font-weight:900">₹'+(t.entry_premium||0)+'</div></div>'
        +'<div style="background:var(--bg2);padding:10px 12px"><div class="mi-l">LIVE P&L</div><div style="font-family:var(--cond);font-size:18px;font-weight:900;color:'+fc(lp)+'">'+fp(lp)+'</div></div>'
        +'<div style="background:var(--bg2);padding:10px 12px"><div class="mi-l">STOP LOSS</div><div style="font-family:var(--cond);font-size:16px;font-weight:900;color:var(--red)">₹'+Math.round(t.sl||0).toLocaleString('en-IN')+'</div></div>'
        +'<div style="background:var(--bg2);padding:10px 12px"><div class="mi-l">TARGET 1</div><div style="font-family:var(--cond);font-size:16px;font-weight:900;color:var(--teal)">₹'+Math.round(t.t1||0).toLocaleString('en-IN')+'</div></div>'
        +'</div>';
    } else {
      if(timeEl) timeEl.textContent='';
      body.innerHTML='<div style="text-align:center;color:var(--muted);font-size:10px;padding:16px">No open position — waiting for signal</div>';
    }
  }
  const hist=document.getElementById('pt-history');
  if (hist) {
    if (!d.trades||!d.trades.length) {
      hist.innerHTML='<div style="text-align:center;color:var(--muted);font-size:10px;padding:16px">No trades yet</div>';
    } else {
      hist.innerHTML=d.trades.slice(0,30).map(function(t){
        const isBuy=t.otype==='CE';
        const col=t.pnl>=0?'var(--green)':'var(--red)';
        const bg=t.pnl>=0?'rgba(0,230,118,0.04)':'rgba(255,23,68,0.04)';
        return '<div class="trade-row" style="background:'+bg+'">'
          +'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">'
          +'<span style="font-family:var(--cond);font-size:13px;font-weight:900;color:'+(isBuy?'var(--green)':'var(--red)')+'">'+  (isBuy?'BUY CALL':'BUY PUT')+' '+(t.strike||0)+'</span>'
          +'<span style="font-family:var(--cond);font-size:14px;font-weight:900;color:'+col+'">'+fp(t.pnl||0)+'</span>'
          +'</div>'
          +'<div style="font-size:8px;color:var(--muted)">'+(t.time||'')+'→'+(t.exit_time||'—')+' · '+(t.lots||1)+' lot · ₹'+(t.entry_premium||0)+' → ₹'+(t.exit_premium||'—')+' · <span style="color:'+col+'">'+(t.reason||'')+'</span></div>'
          +'</div>';
      }).join('');
    }
  }
}

function renderLearning(d) {
  const daysEl=document.getElementById('learn-days');
  const bodyEl=document.getElementById('learn-body');
  if (!daysEl||!bodyEl||!d||!d.days_recorded) return;
  daysEl.textContent=d.days_recorded+' days';
  const wr=d.overall_win_rate||0;
  let html='<div style="display:flex;justify-content:space-around;padding:8px 0;margin-bottom:8px">'
    +'<div style="text-align:center"><div class="mi-l">OVERALL WR</div><div style="font-family:var(--cond);font-size:18px;font-weight:900;color:'+(wr>=50?'var(--green)':'var(--red)')+'">'+wr+'%</div></div>'
    +'<div style="text-align:center"><div class="mi-l">TOTAL TRADES</div><div style="font-family:var(--cond);font-size:18px;font-weight:900">'+(d.total_trades||0)+'</div></div>'
    +'<div style="text-align:center"><div class="mi-l">TOTAL P&L</div><div style="font-family:var(--cond);font-size:18px;font-weight:900;color:'+fc(d.total_pnl||0)+'">'+fp(d.total_pnl||0)+'</div></div>'
    +'</div>';
  if (d.latest_lessons&&d.latest_lessons.length) {
    html+='<div style="font-size:7px;color:var(--muted);letter-spacing:0.1em;margin-bottom:4px">LATEST LESSONS</div>';
    d.latest_lessons.forEach(l=>{ html+='<div style="font-size:9px;padding:2px 0;border-bottom:1px solid var(--bdr)">→ '+l+'</div>'; });
  }
  bodyEl.innerHTML=html;
}

async function manualClose() {
  if (!confirm('Close open trade now?')) return;
  await fetch('/api/trades/close',{method:'POST'});
  fetchTrades();
}

async function resetAccount() {
  if (!confirm('Reset all trades and start fresh with ₹1,00,000?')) return;
  await fetch('/api/trades/reset',{method:'POST'});
  fetchTrades();
}

function checkPaperTrade(sig) {
  if (!sig || sig.signal==='WAIT') return;
  const now5min = Math.floor(Date.now()/300000);
  const key = sig.signal+'_'+sig.strike+'_'+now5min;
  if (key===lastSignalFired) return;
  const conf = sig.conf || 65;
  if (conf >= 55) {
    lastSignalFired = key;
    fetch('/api/trades/signal',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({signal:sig.signal==='BUY'?'BUY':'SELL',otype:sig.otype,strike:sig.strike,sl:sig.sl,t1:sig.t1,t2:sig.t2,conf})
    }).then(function(){
      fetchTrades();
      const dir = sig.signal==='BUY'?'BUY CALL':'BUY PUT';
      TradeBrain.start(sig.signal==='BUY'?'LONG':'SHORT', S.spot, sig.sl||S.spot);
      toast('Trade opened: '+dir+' '+(sig.strike||0).toLocaleString('en-IN')+' @ '+conf+'%');
    });
  }
}

// ═══════════════ ARIA ═══════════════
const ARIA_SYS = 'You are ARIA, Bank Nifty options trading assistant. Be brief (2-4 sentences). No markdown.';
async function askAria() {
  const inp = document.getElementById('aria-input');
  const q = inp.value.trim();
  if (!q) return;
  inp.value = '';
  const msgs = document.getElementById('aria-msgs');
  msgs.innerHTML += '<div class="aria-msg user">You: '+q+'</div>';
  msgs.scrollTop = msgs.scrollHeight;
  try {
    const mktCtx = 'BN: ₹'+f(S.spot)+' VIX:'+S.vix+' Signal:'+(S.signal&&S.signal.signal||'WAIT')+' Trend:'+(S.signal&&S.signal.behaviour||'—');
    const res = await fetch('/aria', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,context:mktCtx})});
    const d = await res.json();
    msgs.innerHTML += '<div class="aria-msg bot">ARIA: '+(d.answer||'...')+'</div>';
    msgs.scrollTop = msgs.scrollHeight;
  } catch(e) {
    msgs.innerHTML += '<div class="aria-msg bot" style="color:var(--red)">ARIA: Error connecting</div>';
  }
}

// ═══════════════ RISK CALCULATOR ═══════════════
['rc-c','rc-e','rc-s'].forEach(id=>{
  const el=document.getElementById(id);
  if(el) el.addEventListener('input',()=>{
    const cap=parseFloat(document.getElementById('rc-c').value)||0;
    const ent=parseFloat(document.getElementById('rc-e').value)||0;
    const sl=parseFloat(document.getElementById('rc-s').value)||0;
    if(!cap||!ent||!sl){document.getElementById('rc-out').textContent='';return;}
    const risk=ent-sl; const lots=Math.floor((cap*0.01)/Math.max(risk*15,1));
    document.getElementById('rc-out').textContent='Risk/lot: ₹'+Math.round(risk*15)+' | Lots: '+Math.max(1,lots)+' | Total risk: ₹'+Math.round(Math.max(1,lots)*risk*15);
  });
});

// ═══════════════ BOOT ═══════════════
setConn(true, false, '');
fetchFromServer();
setInterval(fetchFromServer, 5000);
setInterval(fetchTrades, 10000);
setTimeout(()=>location.reload(), 4*60*60*1000);
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
    response = dict(cache)
    # Always include the last known spot price
    if response.get("spot", 0) == 0 and cache.get("last_session", {}).get("spot", 0) > 0:
        ls = cache["last_session"]
        for key in ["spot","change","pct","high","low","open","vwap",
                    "vix","pcr","max_pain","tot_ce_oi","tot_pe_oi",
                    "sp500_chg","crude_chg","gold_chg","usdinr","option_chain"]:
            if ls.get(key):
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
