# ==================================================================
#  SMART-EDGE SCANNER  —  SINGLE FILE, NO AI, NO DEPENDENCY (but requests)
# ------------------------------------------------------------------
#  Goal: not just "the leaderboard everyone has", but the 5 edges that
#  a raw leaderboard does NOT give you, combined into one signal:
#
#   1) TIMING      — a skilled wallet OPENING a NEW, still-cheap position now
#   2) SPECIALIST  — wallet judged by per-CATEGORY skill, not just total P&L
#   3) CONSENSUS   — 2+ independent skilled wallets agreeing on the same bet
#   4) UNCROWDED   — skilled wallets NOT on the public leaderboard (uncrowded)
#   5) FEASIBLE    — order-book depth check so you can actually enter (slippage)
#
#  Uses: Gamma /markets, Data /trades, /v1/leaderboard, /holders,
#        CLOB /book.  All public, no auth.
#
#  Run:  pip install requests   then   python smart_edge.py
# ==================================================================
import requests, time, math, json, csv
from collections import defaultdict

# ============================ CONFIG ============================
N_LIVE_MARKETS      = 40      # top live markets (by volume) to scan
TRADES_PER_MARKET   = 300     # recent trades pulled per live market
LIVE_LOOKBACK_HRS   = 48      # only count NEW live bets newer than this (TIMING)
MIN_LIVE_NOTIONAL   = 200.0   # ignore tiny live bets
MAX_WALLETS_PROFILE = 90      # cap deep-profiles (speed)
TOP_SIGNALS         = 25      # how many signals to print

# wallet track-record bars (a wallet qualifies via OVERALL or any CATEGORY)
WALLET_LOOKBACK_DAYS = 365
MAX_MARKETS_PER_WALLET = 100000  # effectively no cap (full accuracy)
MIN_N_MARKETS        = 3      # overall resolved markets needed   [LOOSENED]
MIN_SKILL_Z          = 1.0    # overall skill bar                 [LOOSENED]
MIN_CAT_N            = 2      # resolved markets in a category to call it a specialty [LOOSENED]
MIN_CAT_Z            = 1.0    # per-category skill bar (SPECIALIST edge) [LOOSENED]
EARLY_MAX_PRICE      = 0.55   # "cheap / early" entry threshold (TIMING)
MAX_SIGNAL_PRICE     = 0.90   # ignore near-certain bets (no profit left)

# consensus (CONSENSUS edge)
CONSENSUS_MIN_WALLETS = 2

# feasibility / slippage (FEASIBLE edge)
USE_ORDERBOOK    = True
SLIP_TOLERANCE   = 0.03       # accept asks priced within +3% of entry price
MIN_FEASIBLE_USD = 300.0      # need at least this much takeable size in band

# leaderboard seed (UNCROWDED edge — we mark who is already crowded)
LEADERBOARD_LIMIT = 200
USE_LB_SEED       = True   # follow top leaderboard (proven) wallets into ANY open market
LB_SEED_WALLETS   = 60     # how many top leaderboard wallets to seed
SEED_LOOKBACK_DAYS = 30    # how recent a proven wallet's open-market BUY must be

# holders (direct smart-money per live market, no full trade scan)
USE_HOLDERS        = False   # holders disabled: trade-only signals (fresh entries)
HOLDERS_PER_MARKET = 25

# scoring weights
W_SKILL     = 1.0
W_TIMING    = 2.0
W_CONSENSUS = 2.5
W_UNCROWDED = 1.5
TIER_STRONG = 6.0
TIER_MEDIUM = 3.5

# profit-estimate edge model (nudge shrinks toward 0 near price extremes)
K_EDGE           = 0.35
K_CONSENSUS_EDGE = 0.10
MAX_EDGE         = 0.25

# ============================ ENDPOINTS ============================
GAMMA = "https://gamma-api.polymarket.com/markets"
DATA  = "https://data-api.polymarket.com/trades"
LB_URLS = ["https://data-api.polymarket.com/v1/leaderboard",
           "https://data-api.polymarket.com/leaderboard"]
HOLDERS = "https://data-api.polymarket.com/holders"
CLOB_BOOK = "https://clob.polymarket.com/book"
DAY = 86400
SLEEP = 0.2
S = requests.Session(); S.headers.update({"User-Agent": "smart-edge/1.0"})

# ============================ HELPERS ============================
def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(SLEEP * (i + 1))
    return None

def as_list(x):
    if x is None: return []
    if isinstance(x, list): return x
    try: return json.loads(x)
    except Exception: return [x]

def tstamp(t):
    v = t.get("timestamp") or t.get("time") or 0
    try: return float(v)
    except Exception: return 0.0

def winning_index(m):
    prices = as_list(m.get("outcomePrices")); best, bi = -1, None
    for i, p in enumerate(prices):
        try: p = float(p)
        except Exception: continue
        if p > best: best, bi = p, i
    return bi if best >= 0.99 else None

def extract_category(m):
    c = m.get("category")
    if c: return str(c)
    tags = m.get("tags")
    if isinstance(tags, list) and tags:
        f = tags[0]
        if isinstance(f, dict): return str(f.get("label") or f.get("name") or f.get("slug") or "")
        return str(f)
    evs = m.get("events")
    if isinstance(evs, list) and evs and isinstance(evs[0], dict):
        ev = evs[0]; et = ev.get("tags")
        if isinstance(et, list) and et:
            f = et[0]
            if isinstance(f, dict): return str(f.get("label") or f.get("name") or f.get("slug") or "")
            return str(f)
        if ev.get("title") or ev.get("slug"): return str(ev.get("title") or ev.get("slug"))
    return "Uncategorized"

_MKT_CACHE = {}
def market_info(cond):
    if cond in _MKT_CACHE: return _MKT_CACHE[cond]
    info = {"win": None, "volume": 0.0, "question": "", "resolved": False, "category": "Uncategorized"}
    for params in ({"condition_ids": cond}, {"conditionId": cond}):
        data = get(GAMMA, params)
        arr = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
        if arr:
            m = arr[0]
            try: info["volume"] = float(m.get("volume") or 0)
            except Exception: pass
            info["question"] = m.get("question") or m.get("slug") or ""
            info["category"] = extract_category(m)
            wi = winning_index(m)
            info["win"] = wi
            info["resolved"] = (str(m.get("closed")).lower() == "true") or (wi is not None)
            break
    _MKT_CACHE[cond] = info
    return info

def fetch_wallet_trades(wallet, max_pages=12, page=500):
    out, off = [], 0
    for _ in range(max_pages):
        tr = get(DATA, {"user": wallet, "limit": page, "offset": off})
        if not tr: break
        out.extend(tr); off += page; time.sleep(SLEEP)
        if len(tr) < page: break
    return out

def _z(wins, sum_p, var):
    sd = math.sqrt(var) if var > 0 else 1e-9
    return (wins - sum_p) / sd

_PROFILE_CACHE = {}
def profile_wallet(wallet):
    """Overall + PER-CATEGORY skill from resolved-market history."""
    if wallet in _PROFILE_CACHE: return _PROFILE_CACHE[wallet]
    cutoff = time.time() - WALLET_LOOKBACK_DAYS * DAY
    trades = fetch_wallet_trades(wallet)
    wm = defaultdict(lambda: {"size": 0.0, "cost": 0.0, "minp": 1.0})
    notionals = []
    for t in trades:
        if (t.get("side") or "").upper() != "BUY": continue
        if tstamp(t) < cutoff: continue
        cond = t.get("conditionId") or t.get("market")
        if not cond: continue
        try:
            oi = int(t.get("outcomeIndex")); price = float(t.get("price")); size = float(t.get("size"))
        except Exception:
            continue
        d = wm[(cond, oi)]
        d["size"] += size; d["cost"] += size * price; d["minp"] = min(d["minp"], price)
        notionals.append(size * price)
    by_mkt = defaultdict(dict)
    for (cond, oi), d in wm.items():
        by_mkt[cond][oi] = d
    # [FAST] cap markets per wallet (keep highest-cost ones) to bound API calls
    conds = sorted(by_mkt.keys(),
                   key=lambda c: sum(x["cost"] for x in by_mkt[c].values()),
                   reverse=True)[:MAX_MARKETS_PER_WALLET]
    # overall + per category accumulators
    o = {"n": 0, "wins": 0, "sum_p": 0.0, "var": 0.0, "cost": 0.0, "pnl": 0.0, "early": 0}
    cats = defaultdict(lambda: {"n": 0, "wins": 0, "sum_p": 0.0, "var": 0.0, "cost": 0.0, "pnl": 0.0})
    n_fetched = 0  # markets whose info came back from the API (question present)
    for cond in conds:
        outs = by_mkt[cond]
        info = market_info(cond)
        if info["question"]: n_fetched += 1
        if not info["resolved"] or info["win"] is None: continue
        oi = max(outs, key=lambda k: outs[k]["size"]); d = outs[oi]
        if d["size"] <= 0 or d["cost"] <= 0: continue
        avg = min(max(d["cost"] / d["size"], 1e-4), 0.9999)
        won = 1 if oi == info["win"] else 0
        payout = d["size"] if won else 0.0
        o["n"] += 1; o["wins"] += won; o["sum_p"] += avg; o["var"] += avg * (1 - avg)
        o["cost"] += d["cost"]; o["pnl"] += payout - d["cost"]
        if won and d["minp"] <= EARLY_MAX_PRICE: o["early"] += 1
        c = cats[info["category"]]
        c["n"] += 1; c["wins"] += won; c["sum_p"] += avg; c["var"] += avg * (1 - avg)
        c["cost"] += d["cost"]; c["pnl"] += payout - d["cost"]
    cat_out = {}
    for cat, c in cats.items():
        if c["n"] <= 0: continue
        cat_out[cat] = {
            "n": c["n"], "z": round(_z(c["wins"], c["sum_p"], c["var"]), 3),
            "hit": round(c["wins"] / c["n"], 3),
            "roi": round(c["pnl"] / c["cost"], 3) if c["cost"] else 0.0,
        }
    prof = {
        "wallet": wallet, "n_markets": o["n"],
        "n_trades": len(trades), "markets_seen": len(by_mkt), "mkts_fetched": n_fetched,
        "hit_rate": round(o["wins"] / o["n"], 3) if o["n"] else 0.0,
        "skill_z": round(_z(o["wins"], o["sum_p"], o["var"]), 3) if o["n"] else 0.0,
        "early_ratio": round(o["early"] / max(o["wins"], 1), 3),
        "roi": round(o["pnl"] / o["cost"], 3) if o["cost"] else 0.0,
        "avg_bet": round(sum(notionals) / len(notionals), 2) if notionals else 0.0,
        "cats": cat_out, "crowded": False,
    }
    _PROFILE_CACHE[wallet] = prof
    return prof

def best_category(prof):
    best = None
    for cat, c in prof["cats"].items():
        if c["n"] >= MIN_CAT_N and c["z"] >= MIN_CAT_Z:
            if best is None or c["z"] > best[1]["z"]: best = (cat, c)
    return best

def wallet_qualifies(prof):
    if prof["n_markets"] >= MIN_N_MARKETS and prof["skill_z"] >= MIN_SKILL_Z:
        return True
    return best_category(prof) is not None

def fetch_leaderboard():
    """Seed set of already-famous wallets (to mark crowded vs uncrowded)."""
    wallets = {}
    for base in LB_URLS:
        for params in ({"window": "all", "limit": LEADERBOARD_LIMIT},
                       {"period": "all", "limit": LEADERBOARD_LIMIT},
                       {"limit": LEADERBOARD_LIMIT}):
            data = get(base, params)
            arr = data if isinstance(data, list) else (
                (data.get("data") or data.get("leaderboard")) if isinstance(data, dict) else None)
            if arr:
                for e in arr:
                    if not isinstance(e, dict): continue
                    w = (e.get("proxyWallet") or e.get("wallet") or "").lower()
                    if w: wallets[w] = True
                if wallets: return wallets
    return wallets

def feasible_usd(token_id, entry_price):
    """Takeable USD on the ask side within +SLIP_TOLERANCE of entry price."""
    if not (USE_ORDERBOOK and token_id): return None
    data = get(CLOB_BOOK, {"token_id": token_id})
    if not isinstance(data, dict): return None
    asks = data.get("asks") or []
    cap = entry_price * (1 + SLIP_TOLERANCE)
    tot = 0.0
    for a in asks:
        try: p = float(a.get("price")); s = float(a.get("size"))
        except Exception: continue
        if p <= cap: tot += p * s
    return tot

def fetch_live_markets(n):
    out = []
    for params in ({"closed": "false", "active": "true", "order": "volume", "ascending": "false", "limit": n},
                   {"closed": "false", "limit": n}, {"active": "true", "limit": n}):
        data = get(GAMMA, params)
        arr = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
        if arr: out = arr; break
    markets = []
    for m in out:
        cond = m.get("conditionId") or m.get("condition_id")
        if not cond: continue
        try: vol = float(m.get("volume") or 0)
        except Exception: vol = 0.0
        markets.append({"cond": cond, "question": m.get("question") or m.get("slug") or "",
                        "volume": vol, "category": extract_category(m),
                        "prices": as_list(m.get("outcomePrices")),
                        "tokenIds": as_list(m.get("clobTokenIds"))})
    return markets

def fetch_market_trades(cond, limit):
    out, off = [], 0
    while len(out) < limit:
        tr = get(DATA, {"market": cond, "limit": 500, "offset": off})
        if not tr: break
        out.extend(tr); off += 500; time.sleep(SLEEP)
        if len(tr) < 500: break
    return out[:limit] if limit else out

def fetch_holders(cond, tokenIds=None, limit=HOLDERS_PER_MARKET):
    """Top holders per outcome token for a market (direct smart-money lookup,
    no full trade scan). Returns list of (wallet, outcomeIndex, amount).
    Handles both flat and grouped response shapes."""
    data = get(HOLDERS, {"market": cond, "limit": limit})
    res = []
    if isinstance(data, dict):
        data = data.get("holders") or data.get("data") or []
    if not isinstance(data, list) or not data:
        return res
    first = data[0] if isinstance(data[0], dict) else {}
    grouped = ("holders" in first or "token" in first or "tokenId" in first) and \
              not ("proxyWallet" in first or "wallet" in first)
    def _amt(h):
        try: return float(h.get("amount") or h.get("shares") or h.get("size") or 0)
        except Exception: return 0.0
    def _oi(obj):
        v = obj.get("outcomeIndex")
        try: return int(v)
        except Exception: pass
        tok = obj.get("token") or obj.get("tokenId") or obj.get("asset")
        if tokenIds and tok in tokenIds: return tokenIds.index(tok)
        return None
    if not grouped:
        for h in data:
            if not isinstance(h, dict): continue
            w = (h.get("proxyWallet") or h.get("wallet") or "").lower()
            if not w: continue
            res.append((w, _oi(h), _amt(h)))
        return res
    for g in data:
        if not isinstance(g, dict): continue
        oi = _oi(g)
        for h in (g.get("holders") or []):
            if not isinstance(h, dict): continue
            w = (h.get("proxyWallet") or h.get("wallet") or "").lower()
            if not w: continue
            res.append((w, oi, _amt(h)))
    return res

# ============================ SCORING ============================
def score_bet(bet, prof, consensus_n, feasible):
    reasons = []
    cat = bet["cat"]
    cstat = prof["cats"].get(cat)
    if cstat and cstat["n"] >= MIN_CAT_N:
        skill_z = cstat["z"]; basis = "category:" + cat
    else:
        skill_z = prof["skill_z"]; basis = "overall"
    s_skill = max(skill_z, 0.0) * W_SKILL
    if skill_z >= MIN_CAT_Z:
        reasons.append("skilled (" + basis + " z=" + str(round(skill_z, 2)) + ")")
    # TIMING: new + cheap entry (only genuinely new trades, not standing holdings)
    s_time = 0.0
    if bet.get("source") == "trade" and bet["price"] <= EARLY_MAX_PRICE:
        s_time = (EARLY_MAX_PRICE - bet["price"]) / EARLY_MAX_PRICE * W_TIMING
        reasons.append("early/cheap entry @ " + str(bet["price"]))
    # CONSENSUS
    s_cons = 0.0
    if consensus_n >= CONSENSUS_MIN_WALLETS:
        s_cons = (consensus_n - 1) * W_CONSENSUS
        reasons.append(str(consensus_n) + " skilled wallets agree")
    # UNCROWDED
    s_unc = 0.0
    if not prof["crowded"]:
        s_unc = W_UNCROWDED
        reasons.append("uncrowded (not on leaderboard)")
    # FEASIBLE / slippage gate
    feas_ok = (feasible is None) or (feasible >= MIN_FEASIBLE_USD)
    if feasible is not None:
        reasons.append("book depth ~$" + str(int(feasible)) + " in band")
    total = s_skill + s_time + s_cons + s_unc
    if not feas_ok:
        total *= 0.3
        reasons.append("LOW liquidity - hard to enter")
    return round(total, 2), basis, reasons, feas_ok

def tier(score):
    if score >= TIER_STRONG: return "\U0001F534 STRONG"
    if score >= TIER_MEDIUM: return "\U0001F7E0 MEDIUM"
    return "\U0001F7E1 WEAK"

def profit_metrics(bet, prof, consensus_n):
    """Profit potential if you ENTER NOW at the current price.
    payout_mult = 1/price (gross), upside% = profit if it WINS,
    est_win_prob = price nudged up by smart-money edge (bounded ESTIMATE),
    ev% = edge-adjusted expected profit."""
    price = min(max(bet["price"], 0.001), 0.999)
    payout = 1.0 / price
    upside = (payout - 1.0) * 100.0
    cstat = prof["cats"].get(bet["cat"])
    z = cstat["z"] if (cstat and cstat["n"] >= MIN_CAT_N) else prof["skill_z"]
    # edge nudge over the market price, scaled by 'room' = price*(1-price)
    # so it vanishes near 0/1 (no edge where the price is already near-certain)
    room = price * (1.0 - price)
    edge = K_EDGE * max(z, 0.0) * room
    if consensus_n >= 2:
        edge += K_CONSENSUS_EDGE * (consensus_n - 1) * room
    edge = min(edge, MAX_EDGE)
    est_p = min(max(price + edge, 0.0), 0.99)
    ev = (est_p * payout - 1.0) * 100.0
    return payout, round(upside, 1), round(est_p, 3), round(ev, 1)

# ============================ MAIN ============================
CSV_HEADER = ["score", "tier", "wallet", "source", "crowded", "category", "market", "outcomeIndex",
              "price", "notional", "payout_mult", "upside_pct", "est_win_prob", "ev_pct",
              "skill_z", "hit_rate", "roi", "n_markets",
              "consensus_wallets", "feasible_usd", "basis", "reasons"]

def write_empty_csv():
    """Always leave a CSV behind (header only) so artifact upload never fails."""
    with open("smart_edge_signals.csv", "w", newline="") as f:
        csv.writer(f).writerow(CSV_HEADER)

def main():
    print("=== SMART-EDGE SCANNER (timing + specialist + consensus + uncrowded + feasible) ===")
    write_empty_csv()  # placeholder; overwritten at the end if signals are found
    leaderboard = fetch_leaderboard()
    print("leaderboard seed wallets: " + str(len(leaderboard)) + ("" if leaderboard else " (none - everyone treated as uncrowded)"))
    markets = fetch_live_markets(N_LIVE_MARKETS)
    print("live markets: " + str(len(markets)))
    if not markets:
        print("(no live markets - network/API issue)"); return

    cutoff = time.time() - LIVE_LOOKBACK_HRS * 3600
    live_by_wallet = {}
    for i, mk in enumerate(markets, 1):
        for t in fetch_market_trades(mk["cond"], TRADES_PER_MARKET):
            if (t.get("side") or "").upper() != "BUY": continue
            if tstamp(t) < cutoff: continue
            try:
                price = float(t.get("price")); size = float(t.get("size")); oi = int(t.get("outcomeIndex"))
            except Exception:
                continue
            if price > MAX_SIGNAL_PRICE: continue
            notional = size * price
            if notional < MIN_LIVE_NOTIONAL: continue
            w = (t.get("proxyWallet") or "").lower()
            if not w: continue
            live_by_wallet.setdefault(w, []).append({
                "cond": mk["cond"], "title": mk["question"], "cat": mk["category"],
                "oi": oi, "price": round(price, 3), "notional": round(notional, 2),
                "token": t.get("asset") or "", "ts": tstamp(t), "source": "trade",
            })
        if i % 5 == 0 or i == len(markets):
            print("  scanned " + str(i) + "/" + str(len(markets)) + " | candidate wallets: " + str(len(live_by_wallet)))

    # ---- HOLDERS: direct smart-money per live market (no full trade scan) ----
    holder_count = 0
    if USE_HOLDERS:
        for i, mk in enumerate(markets, 1):
            prices = mk.get("prices") or []
            tokenIds = mk.get("tokenIds") or []
            for (w, oi, amt) in fetch_holders(mk["cond"], tokenIds=tokenIds):
                if oi is None or oi >= len(prices): continue
                try: price = float(prices[oi])
                except Exception: continue
                if price > MAX_SIGNAL_PRICE: continue
                notional = amt * price
                if notional < MIN_LIVE_NOTIONAL: continue
                token = tokenIds[oi] if oi < len(tokenIds) else ""
                live_by_wallet.setdefault(w, []).append({
                    "cond": mk["cond"], "title": mk["question"], "cat": mk["category"],
                    "oi": oi, "price": round(price, 3), "notional": round(notional, 2),
                    "token": token, "ts": time.time(), "source": "holder",
                })
                holder_count += 1
            if i % 10 == 0 or i == len(markets):
                print("  holders scanned " + str(i) + "/" + str(len(markets)) + " | holder rows added: " + str(holder_count))

    # ---- LEADERBOARD SEED: follow proven wallets into ANY open market they just entered ----
    if USE_LB_SEED and leaderboard:
        seed_cut = time.time() - SEED_LOOKBACK_DAYS * DAY
        seeds = list(leaderboard.keys())[:LB_SEED_WALLETS]
        seed_added = 0
        for idx, w in enumerate(seeds, 1):
            best_by_mkt = {}
            for t in fetch_wallet_trades(w, max_pages=2):
                if (t.get("side") or "").upper() != "BUY": continue
                if tstamp(t) < seed_cut: continue
                cond = t.get("conditionId") or t.get("market")
                if not cond: continue
                try:
                    price = float(t.get("price")); size = float(t.get("size")); oi = int(t.get("outcomeIndex"))
                except Exception:
                    continue
                if price > MAX_SIGNAL_PRICE: continue
                notional = size * price
                if notional < MIN_LIVE_NOTIONAL: continue
                info = market_info(cond)
                if info["resolved"] or info["win"] is not None: continue   # only still-OPEN markets
                k = (cond, oi)
                cur = best_by_mkt.get(k)
                if cur is None or notional > cur["notional"]:
                    best_by_mkt[k] = {
                        "cond": cond, "title": info["question"] or cond, "cat": info["category"],
                        "oi": oi, "price": round(price, 3), "notional": round(notional, 2),
                        "token": t.get("asset") or "", "ts": tstamp(t), "source": "lb_seed",
                    }
            for b in best_by_mkt.values():
                live_by_wallet.setdefault(w, []).append(b)
                seed_added += 1
            if idx % 10 == 0 or idx == len(seeds):
                print("  leaderboard-seed scanned " + str(idx) + "/" + str(len(seeds)) + " | live bets found: " + str(seed_added), flush=True)

    if not live_by_wallet:
        print("No fresh live bets / holders above $" + str(int(MIN_LIVE_NOTIONAL)) + ". Lower MIN_LIVE_NOTIONAL."); return

    ranked = sorted(live_by_wallet.items(), key=lambda kv: sum(b["notional"] for b in kv[1]), reverse=True)[:MAX_WALLETS_PROFILE]
    print("\nProfiling " + str(len(ranked)) + " wallets (overall + per-category)...")
    qualifying = []  # (prof, bet)
    for j, (w, bets) in enumerate(ranked, 1):
        print("  [" + str(j) + "/" + str(len(ranked)) + "] profiling " + w + " ...", flush=True)
        prof = profile_wallet(w)
        prof["crowded"] = w in leaderboard
        print("      -> trades=" + str(prof["n_trades"]) + " mkts_seen=" + str(prof["markets_seen"])
              + " fetched=" + str(prof.get("mkts_fetched", 0))
              + " resolved=" + str(prof["n_markets"]) + " | z=" + str(prof["skill_z"])
              + " hit=" + str(prof["hit_rate"])
              + (" | QUALIFIES" if wallet_qualifies(prof) else ""), flush=True)
        if wallet_qualifies(prof):
            # dedup same (market, outcome): prefer trade (has timing) over holder
            best = {}
            for b in bets:
                k = (b["cond"], b["oi"])
                cur = best.get(k)
                if cur is None:
                    best[k] = b
                elif cur.get("source") != "trade" and b.get("source") == "trade":
                    best[k] = b
                elif b.get("source") == cur.get("source") and b["notional"] > cur["notional"]:
                    best[k] = b
            for b in best.values():
                qualifying.append((prof, b))

    if not qualifying:
        print("\nNo skilled wallets (overall or per-category) found among active live bettors right now.")
        return

    # CONSENSUS map: (cond, oi) -> distinct skilled wallets
    cons = defaultdict(set)
    for prof, b in qualifying:
        cons[(b["cond"], b["oi"])].add(prof["wallet"])

    # score every qualifying live bet
    scored = []
    for prof, b in qualifying:
        cn = len(cons[(b["cond"], b["oi"])])
        feas = feasible_usd(b["token"], b["price"])
        sc, basis, reasons, feas_ok = score_bet(b, prof, cn, feas)
        scored.append((sc, prof, b, basis, reasons, cn, feas))
    scored.sort(key=lambda x: x[0], reverse=True)

    print("\n========================================================")
    print("TOP SMART-EDGE SIGNALS")
    print("========================================================")
    seen = set()
    shown = 0
    for sc, prof, b, basis, reasons, cn, feas in scored:
        key = (prof["wallet"], b["cond"], b["oi"])
        if key in seen: continue
        seen.add(key)
        shown += 1
        if shown > TOP_SIGNALS: break
        print("\n" + tier(sc) + "  score=" + str(sc) + "  | " + prof["wallet"] + "  [" + b.get("source", "trade") + "]")
        print("   [" + str(b["cat"]) + "] " + str(b["title"])[:64] + "  -> outcome " + str(b["oi"]))
        print("   bet $" + str(int(b["notional"])) + " @ " + str(b["price"])
              + "  | record z=" + str(prof["skill_z"]) + " hit=" + str(prof["hit_rate"])
              + " roi=" + str(prof["roi"]) + " n=" + str(prof["n_markets"]))
        _pm, _up, _ep, _ev = profit_metrics(b, prof, cn)
        print("   if ENTER NOW: " + str(round(_pm, 2)) + "x if wins (+" + str(_up) + "% profit)"
              + "  | est win ~" + str(int(_ep * 100)) + "%  | EV ~" + str(_ev) + "%")
        print("   why: " + "; ".join(reasons))

    # ---- BEST PROFIT IF YOU ENTER NOW (sorted by edge-adjusted EV) ----
    print("\n========= BEST PROFIT IF YOU ENTER NOW (edge-adjusted) =========")
    print("(payout multiple = gross if it wins; EV = adjusted by smart-money edge - an ESTIMATE)")
    prof_rows = []
    seenp = set()
    for sc, prof, b, basis, reasons, cn, feas in scored:
        key = (prof["wallet"], b["cond"], b["oi"])
        if key in seenp: continue
        seenp.add(key)
        pm, up, ep, ev = profit_metrics(b, prof, cn)
        feas_ok = (feas is None) or (feas >= MIN_FEASIBLE_USD)
        prof_rows.append((ev, up, pm, ep, prof, b, cn, feas_ok))
    prof_rows.sort(key=lambda x: x[0], reverse=True)
    for ev, up, pm, ep, prof, b, cn, feas_ok in prof_rows[:TOP_SIGNALS]:
        tag = "" if feas_ok else "  (LOW liquidity - hard to enter)"
        print("\n  EV ~" + str(ev) + "%  | " + str(round(pm, 2)) + "x if wins (+" + str(up) + "%)  | est win ~" + str(int(ep * 100)) + "%" + tag)
        print("     [" + str(b["cat"]) + "] " + str(b["title"])[:60] + " -> outcome " + str(b["oi"]) + "  @ " + str(b["price"]))
        print("     " + prof["wallet"] + "  z=" + str(prof["skill_z"]) + " hit=" + str(prof["hit_rate"]) + ("  | " + str(cn) + " wallets agree" if cn >= 2 else ""))

    # category concentration of the skilled money
    cat_stat = defaultdict(lambda: {"n": 0, "usd": 0.0})
    for sc, prof, b, basis, reasons, cn, feas in scored:
        c = str(b["cat"]) or "Uncategorized"
        cat_stat[c]["n"] += 1; cat_stat[c]["usd"] += b["notional"]
    print("\n--- Where the skilled money is concentrated (by category) ---")
    for c, st in sorted(cat_stat.items(), key=lambda kv: kv[1]["usd"], reverse=True)[:12]:
        print("  " + c[:18].ljust(18) + " bets=" + str(st["n"]) + "  total=$" + str(int(st["usd"])))

    with open("smart_edge_signals.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["score", "tier", "wallet", "source", "crowded", "category", "market", "outcomeIndex",
                     "price", "notional", "payout_mult", "upside_pct", "est_win_prob", "ev_pct",
                     "skill_z", "hit_rate", "roi", "n_markets",
                     "consensus_wallets", "feasible_usd", "basis", "reasons"])
        seen2 = set()
        for sc, prof, b, basis, reasons, cn, feas in scored:
            key = (prof["wallet"], b["cond"], b["oi"])
            if key in seen2: continue
            seen2.add(key)
            pm, up, ep, ev = profit_metrics(b, prof, cn)
            wr.writerow([sc, tier(sc), prof["wallet"], b.get("source", "trade"), prof["crowded"], b["cat"], str(b["title"])[:100],
                         b["oi"], b["price"], b["notional"], round(pm, 3), up, ep, ev,
                         prof["skill_z"], prof["hit_rate"],
                         prof["roi"], prof["n_markets"], cn, ("" if feas is None else int(feas)),
                         basis, " | ".join(reasons)])
    print("\nSaved -> smart_edge_signals.csv")

if __name__ == "__main__":
    main()
