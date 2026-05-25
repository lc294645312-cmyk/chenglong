from flask import Flask, render_template, request, jsonify
import requests
import json
import os
import threading
import time
from datetime import datetime

app = Flask(__name__)

ALERTS_FILE = 'alerts.json'
HISTORY_FILE = 'history.json'
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

price_cache = {'BTC': 0, 'ETH': 0, 'DOGE': 0, 'BCH': 0, 'ZEC': 0, 'last_update': ''}

COIN_CONFIG = {
    'BTC':  {'symbol': 'BTCUSDT',  'cg_id': 'bitcoin',      'name': '比特币'},
    'ETH':  {'symbol': 'ETHUSDT',  'cg_id': 'ethereum',     'name': '以太坊'},
    'DOGE': {'symbol': 'DOGEUSDT', 'cg_id': 'dogecoin',     'name': '狗狗币'},
    'BCH':  {'symbol': 'BCHUSDT',  'cg_id': 'bitcoin-cash', 'name': '比特币现金'},
    'ZEC':  {'symbol': 'ZECUSDT',  'cg_id': 'zcash',        'name': 'Zcash'},
}

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; CryptoBot/1.0)'}

def load_json(f, d):
    try:
        if os.path.exists(f):
            with open(f) as fp: return json.load(fp)
    except: pass
    return d

def save_json(f, d):
    with open(f, 'w') as fp: json.dump(d, fp, ensure_ascii=False, indent=2)

def get_ticker(coin):
    symbol = COIN_CONFIG[coin]['symbol']
    cg_id  = COIN_CONFIG[coin]['cg_id']
    urls = [
        f'https://api.binance.us/api/v3/ticker/24hr?symbol={symbol}',
        f'https://api1.binance.com/api/v3/ticker/24hr?symbol={symbol}',
        f'https://api2.binance.com/api/v3/ticker/24hr?symbol={symbol}',
        f'https://api3.binance.com/api/v3/ticker/24hr?symbol={symbol}',
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10, headers=HEADERS)
            if r.status_code == 200:
                d = r.json()
                if 'lastPrice' in d:
                    price = float(d['lastPrice'])
                    return {
                        'price':       price,
                        'change':      float(d['priceChangePercent']),
                        'high':        float(d['highPrice']),
                        'low':         float(d['lowPrice']),
                        'volume':      float(d['volume']),
                        'quoteVolume': float(d['quoteVolume']),
                    }
        except: continue
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price'
        r = requests.get(url, params={'ids':cg_id,'vs_currencies':'usd','include_24hr_change':'true','include_24hr_vol':'true'}, timeout=15, headers=HEADERS)
        if r.status_code == 200:
            d = r.json()[cg_id]
            p = d['usd']
            return {'price':p,'change':d.get('usd_24h_change',0) or 0,'high':p*1.01,'low':p*0.99,'volume':d.get('usd_24h_vol',0)/max(p,0.0001),'quoteVolume':d.get('usd_24h_vol',0)}
    except: pass
    return None

def get_klines(symbol, interval, limit=200):
    urls = [
        f'https://api.binance.us/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}',
        f'https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}',
        f'https://api2.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}',
        f'https://api3.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}',
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10, headers=HEADERS)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 10:
                    return {
                        'closes':  [float(k[4]) for k in data],
                        'highs':   [float(k[2]) for k in data],
                        'lows':    [float(k[3]) for k in data],
                        'volumes': [float(k[5]) for k in data],
                        'times':   [int(k[0]) for k in data],
                    }
        except: continue
    return None

def ma(closes, n):
    if len(closes) < n: return None
    return round(sum(closes[-n:]) / n, 8)

def ema(closes, n):
    if len(closes) < n: return None
    k = 2 / (n + 1)
    e = closes[0]
    for p in closes[1:]: e = p * k + e * (1 - k)
    return round(e, 8)

def rsi(closes, n=14):
    if len(closes) < n + 1: return None
    deltas = [closes[i+1]-closes[i] for i in range(len(closes)-1)]
    g = sum(d for d in deltas[-n:] if d > 0) / n
    l = sum(-d for d in deltas[-n:] if d < 0) / n
    if l == 0: return 100
    return round(100 - 100 / (1 + g/l), 2)

def macd(closes):
    if len(closes) < 35: return None, None, None
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    if not e12 or not e26: return None, None, None
    m = e12 - e26
    ms = []
    for i in range(26, len(closes)+1):
        e1 = ema(closes[:i], 12)
        e2 = ema(closes[:i], 26)
        if e1 and e2: ms.append(e1 - e2)
    sig = ema(ms, 9) if len(ms) >= 9 else m
    return round(m, 8), round(sig or m, 8), round(m - (sig or m), 8)

def bollinger(closes, n=20, k=2):
    if len(closes) < n: return None, None, None
    rv = closes[-n:]
    mid = sum(rv) / n
    std = (sum((x-mid)**2 for x in rv) / n) ** 0.5
    return round(mid - k*std, 8), round(mid, 8), round(mid + k*std, 8)

def atr(highs, lows, closes, n=14):
    if len(closes) < n+1: return None
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    return round(sum(trs[-n:]) / n, 8)

def vol_ratio(volumes):
    if len(volumes) < 21: return None
    avg = sum(volumes[-21:-1]) / 20
    return round(volumes[-1] / avg, 2) if avg > 0 else None

def swing_points(highs, lows, window=5):
    sh, sl = [], []
    for i in range(window, len(highs)-window):
        if highs[i] == max(highs[i-window:i+window+1]): sh.append(highs[i])
        if lows[i] == min(lows[i-window:i+window+1]): sl.append(lows[i])
    return sh[-5:], sl[-5:]

def trend_by_tf(klines, label):
    if not klines: return {'label': label, 'trend': '数据不足', 'score': 50}
    c = klines['closes']
    price = c[-1]
    m20 = ma(c, 20)
    m50 = ma(c, 50)
    r = rsi(c)
    mc, sig, hist = macd(c)
    score = 50
    if m20 and price > m20: score += 10
    elif m20: score -= 10
    if m50 and price > m50: score += 8
    elif m50: score -= 8
    if m20 and m50 and m20 > m50: score += 7
    elif m20 and m50: score -= 7
    if r:
        if r > 60: score += 10
        elif r < 40: score -= 10
    if mc and mc > 0: score += 8
    elif mc: score -= 8
    score = max(0, min(100, score))
    if score >= 65: trend = '上涨 📈'
    elif score <= 35: trend = '下跌 📉'
    else: trend = '震荡 ↔️'
    return {'label': label, 'trend': trend, 'score': score}

def volume_analysis(klines):
    if not klines: return {}
    vols = klines['volumes']
    closes = klines['closes']
    vr = vol_ratio(vols)
    result = {'vol_ratio': vr, 'volume_warning': ''}
    if not vr: return result
    price_change = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
    if price_change > 0.5 and vr >= 2.0:
        result['signal'] = '🚀 放量突破'
        result['signal_detail'] = '成交量是均量' + str(round(vr,1)) + '倍，价格上涨' + str(round(price_change,1)) + '%，突破信号强烈'
    elif price_change > 0 and vr >= 1.5:
        result['signal'] = '📈 温和放量上涨'
        result['signal_detail'] = '量比' + str(vr) + 'x，温和上涨，可关注持续性'
    elif price_change < -0.5 and vr >= 2.0:
        result['signal'] = '🔻 放量下跌'
        result['signal_detail'] = '成交量' + str(round(vr,1)) + '倍，价格下跌' + str(round(price_change,1)) + '%，下行压力大'
        result['volume_warning'] = '放量下跌，短线风险增加'
    elif price_change < -0.3 and vr >= 1.5:
        result['signal'] = '📉 温和放量下跌'
        result['signal_detail'] = '量比' + str(vr) + 'x，价格下跌' + str(round(price_change,1)) + '%，注意风险'
        result['volume_warning'] = '放量下跌，短线风险增加'
    elif price_change > 0.3 and vr < 0.6:
        result['signal'] = '⚠️ 缩量上涨'
        result['signal_detail'] = '量比仅' + str(vr) + 'x，价格上涨' + str(round(price_change,1)) + '%，成交量不支撑'
        result['volume_warning'] = '缩量上涨，追多风险增加'
    elif vr < 0.5:
        result['signal'] = '😴 缩量横盘'
        result['signal_detail'] = '量比仅' + str(vr) + 'x，成交清淡，方向待定'
    else:
        result['signal'] = '➡️ 正常波动'
        result['signal_detail'] = '量比' + str(vr) + 'x，成交量正常'
    return result

def support_resistance(k1h, k4h, k1d, price):
    res, sup = [], []
    labels_res, labels_sup = {}, {}
    def add_level(val, is_res, label):
        if is_res and val > price:
            res.append(val); labels_res[round(val, 4)] = label
        elif not is_res and val < price:
            sup.append(val); labels_sup[round(val, 4)] = label
    for klines, tf in [(k1h, '1H'), (k4h, '4H'), (k1d, '1D')]:
        if not klines: continue
        h, l, c = klines['highs'], klines['lows'], klines['closes']
        n = min(len(h), {'1H':48,'4H':30,'1D':14}[tf])
        add_level(max(h[-n:]), True, tf+'近期高点')
        add_level(min(l[-n:]), False, tf+'近期低点')
        sh, sl = swing_points(h, l)
        for v in sh: add_level(round(v,4), True, tf+'摆动高点')
        for v in sl: add_level(round(v,4), False, tf+'摆动低点')
        for n_ma, name in [(20,'MA20'),(50,'MA50')]:
            v = ma(c, n_ma)
            if v: add_level(v, v > price, tf+name)
    def merge_levels(levels):
        if not levels: return []
        levels = sorted(set(round(l, 4) for l in levels))
        merged = [levels[0]]
        for l in levels[1:]:
            if abs(l - merged[-1]) / merged[-1] > 0.015: merged.append(l)
        return merged
    res_m = merge_levels(res)[:4]
    sup_m = merge_levels(sorted(sup, reverse=True))[:4]
    return {
        'resistance': [{'price': r, 'label': labels_res.get(round(r,4), '压力位'), 'dist': round((r-price)/price*100,2)} for r in res_m],
        'support':    [{'price': s, 'label': labels_sup.get(round(s,4), '支撑位'), 'dist': round((s-price)/price*100,2)} for s in sup_m],
    }

def predict_range(k1h, price, atr_val):
    if not k1h or not atr_val:
        return {'high': round(price*1.02,4), 'low': round(price*0.98,4), 'basis': '基于2%估算'}
    highs = k1h['highs']
    lows = k1h['lows']
    pred_high = round(price + atr_val * 2.5, 4)
    pred_low  = round(price - atr_val * 2.5, 4)
    near_res = [h for h in highs[-48:] if h > price]
    near_sup = [l for l in lows[-48:] if l < price]
    if near_res: pred_high = min(pred_high, round(min(near_res)*1.002, 4))
    if near_sup: pred_low  = max(pred_low,  round(max(near_sup)*0.998, 4))
    rng = round((pred_high - pred_low) / price * 100, 2)
    return {'high': pred_high, 'low': pred_low, 'range_pct': rng, 'basis': 'ATR预测区间' + str(rng) + '%'}
def calc_scores(data):
    price = data.get('price', 0)
    scores = {}

    tf_list = data.get('timeframe_trends', [])
    tf_scores = [t['score'] for t in tf_list if 'score' in t]
    trend_score = round(sum(tf_scores) / len(tf_scores)) if tf_scores else 50
    scores['trend'] = trend_score

    bull_tfs = sum(1 for t in tf_list if t.get('score', 50) >= 60)
    bear_tfs  = sum(1 for t in tf_list if t.get('score', 50) <= 40)
    total_tfs = len(tf_list) if tf_list else 1
    resonance = max(bull_tfs, bear_tfs) / total_tfs
    if bull_tfs >= bear_tfs:
        tf_score = round(50 + resonance * 50)
    else:
        tf_score = round(50 - resonance * 50)
    tf_score = max(0, min(100, tf_score))
    scores['timeframe'] = tf_score

    rsi_1h = data.get('rsi_1h')
    rsi_4h = data.get('rsi_4h')
    macd_h = data.get('macd_hist_1h')
    mom = 50
    if rsi_1h:
        if rsi_1h > 75:   mom += 8
        elif rsi_1h > 70: mom += 12
        elif rsi_1h > 60: mom += 18
        elif rsi_1h > 50: mom += 6
        elif rsi_1h < 25: mom -= 8
        elif rsi_1h < 30: mom -= 12
        elif rsi_1h < 40: mom -= 18
        elif rsi_1h < 50: mom -= 6
    if rsi_4h:
        if rsi_4h > 60:   mom += 8
        elif rsi_4h < 40: mom -= 8
    if macd_h:
        if macd_h > 0: mom += 12
        else:          mom -= 12
    scores['momentum'] = max(0, min(100, round(mom)))

    vr = data.get('vol_ratio_1h')
    vol_sig = data.get('volume_signal', '')
    vol_warn = data.get('volume_warning', '')
    vol = 50
    if vr:
        if vr >= 2.0:
            vol = 85 if ('上涨' in vol_sig or '突破' in vol_sig) else 20
        elif vr >= 1.5:
            vol = 72 if price > (data.get('ma20_1h') or price) else 35
        elif vr >= 0.8:
            vol = 58
        elif vr >= 0.5:
            vol = 45
        elif vr >= 0.3:
            vol = 32
        else:
            vol = 20
    scores['volume'] = max(0, min(100, round(vol)))

    rsi_v = rsi_1h or 50
    bb_upper = data.get('bb_upper_1h')
    bb_lower = data.get('bb_lower_1h')
    risk = 30
    if rsi_v > 78:    risk += 30
    elif rsi_v > 72:  risk += 18
    elif rsi_v > 68:  risk += 8
    elif rsi_v < 22:  risk += 30
    elif rsi_v < 28:  risk += 18
    elif rsi_v < 32:  risk += 8
    if bb_upper and price > bb_upper * 1.005: risk += 20
    elif bb_upper and price > bb_upper * 0.99: risk += 10
    if bb_lower and price < bb_lower * 0.995: risk += 20
    elif bb_lower and price < bb_lower * 1.01: risk += 10
    change = abs(data.get('change_24h', 0))
    if change > 10:   risk += 20
    elif change > 8:  risk += 12
    elif change > 5:  risk += 6
    if vol_warn:      risk += 10
    scores['risk'] = max(0, min(100, round(risk)))

    raw_signal = (
        trend_score        * 0.30 +
        scores['momentum'] * 0.20 +
        scores['volume']   * 0.20 +
        tf_score           * 0.15 -
        scores['risk']     * 0.15
    )
    raw_signal = max(0, min(100, round(raw_signal)))

    vol_cap = 100
    vol_cap_reason = ''
    if vr is not None:
        if vr < 0.3:
            vol_cap = 55
            vol_cap_reason = '量比<0.3，信号上限55'
        elif vr < 0.5:
            vol_cap = 65
            vol_cap_reason = '量比<0.5，信号上限65'
        elif vr < 0.8:
            vol_cap = 72
            vol_cap_reason = '量比<0.8，信号上限72'
    signal = min(raw_signal, vol_cap)
    scores['signal'] = signal
    scores['vol_cap'] = vol_cap
    scores['vol_cap_reason'] = vol_cap_reason

    if signal >= 82:   sig_dir = '强多';  sig_text = '强烈看多 🚀'; sig_cls = 'bull'
    elif signal >= 65: sig_dir = '看多';  sig_text = '偏多 📈';     sig_cls = 'bull'
    elif signal >= 55: sig_dir = '偏多';  sig_text = '偏多 📈';     sig_cls = 'bull'
    elif signal >= 45: sig_dir = '震荡';  sig_text = '震荡观望 ↔️'; sig_cls = 'neutral'
    elif signal >= 35: sig_dir = '偏空';  sig_text = '偏空 📉';     sig_cls = 'bear'
    elif signal >= 20: sig_dir = '看空';  sig_text = '偏空 📉';     sig_cls = 'bear'
    else:              sig_dir = '强空';  sig_text = '强烈看空 🔻'; sig_cls = 'bear'

    scores['signal_direction'] = sig_dir
    scores['signal_text']  = sig_text
    scores['signal_class'] = sig_cls

    scores['trend_reason']    = '多周期均线趋势' + ('偏强' if trend_score > 60 else '偏弱' if trend_score < 40 else '中性')
    scores['momentum_reason'] = 'RSI=' + str(rsi_1h or '--') + ' MACD' + ('正值' if macd_h and macd_h > 0 else '负值' if macd_h else '计算中')
    scores['volume_reason']   = '量比' + str(vr or '--') + 'x ' + (vol_cap_reason or str(data.get('volume_signal_detail', '')))
    scores['risk_reason']     = 'RSI' + ('超买' if rsi_v > 70 else '超卖' if rsi_v < 30 else '正常') + ' 24H波动' + str(round(abs(data.get('change_24h', 0)), 1)) + '%'
    scores['signal_reason']   = '趋势30%+动能20%+量能20%+多周期15%-风险15%=' + str(signal) + ' ' + (vol_cap_reason or '')
    return scores

def calc_tp_sl(price, atr_val, signal_score, sr):
    if not atr_val: atr_val = price * 0.02
    res_list = sr.get('resistance', [])
    sup_list = sr.get('support', [])

    is_long = signal_score >= 45

    if is_long:
        if sup_list:
            raw_sl = sup_list[0]['price'] * 0.998
            sl_dist_pct = (price - raw_sl) / price * 100
            if sl_dist_pct > 3.0:
                sl = round(price - min(atr_val * 1.5, price * 0.03), 4)
            else:
                sl = round(raw_sl, 4)
        else:
            sl = round(price - min(atr_val * 1.5, price * 0.03), 4)

        tp1 = round(res_list[0]['price'] * 0.998, 4) if res_list else round(price + atr_val * 2.0, 4)
        if len(res_list) > 1:
            tp2_candidate = round(res_list[1]['price'] * 0.998, 4)
            tp2 = tp2_candidate if tp2_candidate > tp1 * 1.005 else round(tp1 + atr_val * 2.0, 4)
        else:
            tp2 = round(price + atr_val * 4.0, 4)

        if sl >= price: sl = round(price - atr_val * 1.5, 4)
        if tp1 <= price: tp1 = round(price + atr_val * 2.0, 4)
        if tp2 <= tp1:   tp2 = round(tp1 + atr_val * 2.0, 4)

    else:
        if res_list:
            raw_sl = res_list[0]['price'] * 1.002
            sl_dist_pct = (raw_sl - price) / price * 100
            if sl_dist_pct > 3.0:
                sl = round(price + min(atr_val * 1.5, price * 0.03), 4)
            else:
                sl = round(raw_sl, 4)
        else:
            sl = round(price + min(atr_val * 1.5, price * 0.03), 4)

        tp1 = round(sup_list[0]['price'] * 1.002, 4) if sup_list else round(price - atr_val * 2.0, 4)
        if len(sup_list) > 1:
            tp2_candidate = round(sup_list[1]['price'] * 1.002, 4)
            tp2 = tp2_candidate if tp2_candidate < tp1 * 0.995 else round(tp1 - atr_val * 2.0, 4)
        else:
            tp2 = round(price - atr_val * 4.0, 4)

        if sl <= price: sl = round(price + atr_val * 1.5, 4)
        if tp1 >= price: tp1 = round(price - atr_val * 2.0, 4)
        if tp2 >= tp1:   tp2 = round(tp1 - atr_val * 2.0, 4)

    sl_dist  = abs(price - sl)
    tp1_dist = abs(tp1 - price)
    tp2_dist = abs(tp2 - price)
    sl_pct   = round(sl_dist / price * 100, 2)
    tp1_pct  = round(tp1_dist / price * 100, 2)
    tp2_pct  = round(tp2_dist / price * 100, 2)
    rr1 = round(tp1_dist / sl_dist, 2) if sl_dist > 0 else 0
    rr2 = round(tp2_dist / sl_dist, 2) if sl_dist > 0 else 0

    valid_trade = False
    rr_note = ''
    if sl_dist <= 0 or tp1_dist <= 0:
        rr_note = '⛔ 止盈止损数据异常，禁止交易'
    elif rr1 < 1.0:
        rr_note = '⛔ 盈亏比<1，观望'
    elif rr1 < 1.5:
        rr_note = '⚠️ 盈亏比1~1.5，不建议开仓'
    elif rr1 < 2.0:
        rr_note = '🟡 盈亏比1.5~2，仅小仓位试单'
        valid_trade = True
    elif rr1 >= 3.0:
        rr_note = '✅ 盈亏比≥3，优质机会（仍需量价确认）'
        valid_trade = True
    else:
        rr_note = '✅ 盈亏比≥2，可交易'
        valid_trade = True

    risk_level = '低' if signal_score >= 60 and sl_pct < 3 else '高' if signal_score < 40 or sl_pct > 5 else '中'

    return {
        'stop_loss':     sl,
        'sl_pct':        sl_pct,
        'take_profit_1': tp1,
        'tp1_pct':       tp1_pct,
        'take_profit_2': tp2,
        'tp2_pct':       tp2_pct,
        'risk_reward':   rr1,
        'risk_level':    risk_level,
        'direction':     'long' if is_long else 'short',
        'risk_reward_result': {
            'entry':         price,
            'stop_loss':     sl,
            'take_profit_1': tp1,
            'take_profit_2': tp2,
            'rr_1':          rr1,
            'rr_2':          rr2,
            'sl_pct':        sl_pct,
            'tp1_pct':       tp1_pct,
            'tp2_pct':       tp2_pct,
            'valid_trade':   valid_trade,
            'note':          rr_note,
        },
    }

def calc_final_action_grade(scores, tp_sl, vr, tf_list):
    signal     = scores.get('signal', 50)
    rr_result  = tp_sl.get('risk_reward_result', {})
    rr1        = rr_result.get('rr_1', 0)
    rr_note    = rr_result.get('note', '')
    vol_cap_r  = scores.get('vol_cap_reason', '')

    if not tf_list or vr is None:
        return {'grade':'E','action':'禁止交易','reason':'关键数据不足，无法评估风险'}
    if '异常' in rr_note or rr1 <= 0:
        return {'grade':'E','action':'禁止交易','reason':'止盈止损逻辑异常：' + rr_note}
    if rr1 < 0.5:
        return {'grade':'E','action':'禁止交易','reason':'盈亏比=' + str(rr1) + '严重不合理'}

    max_grade_num = 6
    if vr < 0.5:
        max_grade_num = min(max_grade_num, 4)
    if rr1 < 1.5:
        max_grade_num = min(max_grade_num, 3)
    bull_tfs = sum(1 for t in tf_list if t.get('score', 50) >= 60)
    bear_tfs  = sum(1 for t in tf_list if t.get('score', 50) <= 40)
    if bull_tfs > 0 and bear_tfs > 0 and abs(bull_tfs - bear_tfs) <= 1:
        max_grade_num = min(max_grade_num, 4)

    if signal >= 72 and rr1 >= 3.0 and vr >= 0.8:
        raw_grade = 6
    elif signal >= 65 and rr1 >= 2.0:
        raw_grade = 5
    elif signal >= 58 and rr1 >= 1.5:
        raw_grade = 4
    elif signal >= 45 and rr1 >= 1.0:
        raw_grade = 3
    elif signal >= 35:
        raw_grade = 2
    else:
        raw_grade = 1

    final_grade_num = min(raw_grade, max_grade_num)
    grade_map  = {6:'A+', 5:'A', 4:'B', 3:'C', 2:'D', 1:'E'}
    action_map = {
        6:'高质量机会，可正常仓位交易',
        5:'可交易，建议标准仓位',
        4:'小仓位试单（≤30%仓位）',
        3:'观望，等待更好机会',
        2:'风险过高，不建议交易',
        1:'禁止交易',
    }
    grade  = grade_map[final_grade_num]
    action = action_map[final_grade_num]
    reasons = ['信号分' + str(signal), '盈亏比' + str(rr1), '量比' + str(round(vr,2)) + 'x']
    if vol_cap_r: reasons.append(vol_cap_r)
    if max_grade_num < raw_grade:
        if rr1 < 1.5:    reasons.append('盈亏比不足限制等级')
        if vr < 0.5:     reasons.append('成交量不足限制等级')
        if bull_tfs > 0 and bear_tfs > 0: reasons.append('多周期冲突限制等级')
    return {'grade':grade,'action':action,'reason':' | '.join(reasons),'rr_note':rr_note,'signal':signal,'rr1':rr1}
def calc_multi_timeframe_signal(tf_list, vr):
    if not tf_list or len(tf_list) < 3:
        return {'status':'数据不足','score':50,'note':'周期数据不足，无法判断共振'}

    scores_map = {t['label']: t['score'] for t in tf_list}
    s15 = scores_map.get('15分钟', 50)
    s1h = scores_map.get('1小时',  50)
    s4h = scores_map.get('4小时',  50)
    s1d = scores_map.get('日线',   50)

    bull15 = s15 >= 58; bull1h = s1h >= 58; bull4h = s4h >= 58; bull1d = s1d >= 58
    bear15 = s15 <= 42; bear1h = s1h <= 42; bear4h = s4h <= 42; bear1d = s1d <= 42

    vol_ok = vr is not None and vr >= 0.8
    bull_count = sum([bull15, bull1h, bull4h, bull1d])
    bear_count = sum([bear15, bear1h, bear4h, bear1d])
    avg = round(sum([s15, s1h, s4h, s1d]) / 4)

    if bull15 and bull1h and bull4h and vol_ok:
        status = '强多头共振'
        score  = min(88, avg + 15)
        note   = '15m/1h/4h全部偏多' + ('且成交量确认' if vol_ok else '') + ('，日线亦共振' if bull1d else '，日线待确认')
    elif bull15 and bull1h and bull4h and not vol_ok:
        status = '弱多头共振'
        score  = min(78, avg + 8)
        note   = '15m/1h/4h偏多，但成交量不足，可信度打折'
    elif bull15 and bull1h and not bull4h and not bear4h:
        status = '弱多头共振'
        score  = min(68, avg + 5)
        note   = '15m和1h偏多，但4h震荡未确认，短线偏多'
    elif bull15 and bull1h and bear4h:
        status = '无共振'
        score  = 50
        note   = '短周期上涨，但4小时下跌，方向冲突'
    elif bear15 and bear1h and bear4h and vol_ok:
        status = '强空头共振'
        score  = max(12, avg - 15)
        note   = '15m/1h/4h全部偏空' + ('且成交量确认' if vol_ok else '') + ('，日线亦共振' if bear1d else '，日线待确认')
    elif bear15 and bear1h and bear4h and not vol_ok:
        status = '弱空头共振'
        score  = max(22, avg - 8)
        note   = '15m/1h/4h偏空，但成交量不足'
    elif bear15 and bear1h and not bear4h and not bull4h:
        status = '弱空头共振'
        score  = max(32, avg - 5)
        note   = '15m和1h偏空，4h震荡，短线偏空'
    elif bull_count >= 3 or bear_count >= 3:
        status = '强多头共振' if bull_count >= 3 else '强空头共振'
        score  = min(80, avg + 10) if bull_count >= 3 else max(20, avg - 10)
        note   = '三个以上周期方向一致'
    else:
        status = '无共振'
        score  = 50
        note   = '多空信号混杂，周期之间方向不一致'

    score = max(0, min(100, score))
    return {'status': status, 'score': score, 'note': note}


def calc_market_regime(tf_list, vr, vol_signal, price, resistance, support, rsi_1h, change_24h):
    if not tf_list:
        return {'type':'无方向震荡','confidence':'低','reason':'数据不足'}

    scores_map = {t['label']: t['score'] for t in tf_list}
    s15 = scores_map.get('15分钟', 50)
    s1h = scores_map.get('1小时',  50)
    s4h = scores_map.get('4小时',  50)
    s1d = scores_map.get('日线',   50)
    avg = (s15 + s1h + s4h + s1d) / 4

    vol_ok   = vr is not None and vr >= 0.8
    vol_high = vr is not None and vr >= 1.8
    vol_low  = vr is not None and vr < 0.5
    volatility_high = abs(change_24h) > 6

    near_res = resistance and len(resistance) > 0 and resistance[0]['dist'] < 1.5
    near_sup = support    and len(support)    > 0 and abs(support[0]['dist']) < 1.5

    reasons = []

    if vol_high and avg >= 65 and s4h >= 60:
        regime = '放量突破'
        conf   = '高' if s1d >= 55 else '中'
        reasons.append('成交量放大' + str(round(vr,1)) + 'x且多周期偏多')
    elif vol_high and avg <= 38:
        regime = '破位下跌'
        conf   = '高'
        reasons.append('放量下跌，均线空头排列')
    elif volatility_high and (rsi_1h and (rsi_1h > 72 or rsi_1h < 28)):
        regime = '高波动风险区'
        conf   = '中'
        reasons.append('24H波动' + str(round(abs(change_24h),1)) + '%，RSI=' + str(rsi_1h))
    elif near_res and vol_ok and s15 >= 60 and s1h < 60:
        regime = '假突破嫌疑'
        conf   = '中'
        reasons.append('价格逼近压力位但1小时未确认，量比' + str(vr) + 'x')
    elif vol_low:
        regime = '缩量横盘'
        conf   = '中' if abs(avg - 50) < 8 else '低'
        reasons.append('量比仅' + str(vr) + 'x，成交清淡')
    elif avg >= 68 and s4h >= 62 and s1d >= 58:
        regime = '趋势上涨'
        conf   = '高' if vol_ok else '中'
        reasons.append('四周期均偏多，趋势明确')
    elif avg <= 32 and s4h <= 38 and s1d <= 42:
        regime = '趋势下跌'
        conf   = '高' if vol_ok else '中'
        reasons.append('四周期均偏空，趋势明确')
    elif avg >= 58 and (s4h >= 55 or s1h >= 62):
        regime = '震荡偏多'
        conf   = '中' if not vol_ok else '中高'
        reasons.append('短周期偏多，大周期尚未完全确认')
    elif avg <= 42 and (s4h <= 45 or s1h <= 38):
        regime = '震荡偏空'
        conf   = '中' if not vol_ok else '中高'
        reasons.append('短周期偏空，大周期尚未完全确认')
    else:
        regime = '无方向震荡'
        conf   = '低'
        reasons.append('多空信号混杂，方向不明')

    if vol_ok and regime not in ['放量突破','破位下跌']:
        reasons.append('成交量支撑')
    if not vol_ok and regime in ['趋势上涨','震荡偏多','放量突破']:
        reasons.append('成交量不足，需谨慎')
        if conf == '高': conf = '中高'
        elif conf == '中高': conf = '中'

    return {'type': regime, 'confidence': conf, 'reason': '；'.join(reasons)}


def calc_fake_breakout_risk(price, resistance, support, vr, rsi_1h, tf_list, macd_hist, k1h_klines):
    risk_score = 0
    factors = []

    if not tf_list:
        return {'level':'无法判断','score':50,'reason':'数据不足','factors':[]}

    scores_map = {t['label']: t['score'] for t in tf_list}
    s15 = scores_map.get('15分钟', 50)
    s1h = scores_map.get('1小时',  50)
    s4h = scores_map.get('4小时',  50)

    if resistance and resistance[0]['dist'] < 1.2:
        risk_score += 25
        factors.append('价格距压力位仅' + str(resistance[0]['dist']) + '%')

    if vr is not None:
        if vr < 0.5:
            risk_score += 25
            factors.append('量比极低(' + str(vr) + 'x)，突破无力')
        elif vr < 0.8:
            risk_score += 12
            factors.append('量比偏低(' + str(vr) + 'x)')

    if rsi_1h and rsi_1h > 70:
        risk_score += 20
        factors.append('RSI=' + str(rsi_1h) + '超买，动能衰竭风险')

    if s15 >= 60 and s1h >= 58 and s4h < 55:
        risk_score += 20
        factors.append('15m/1h上涨但4h未确认')
    elif s15 >= 60 and s1h < 55:
        risk_score += 15
        factors.append('15m上涨但1h未跟进')

    if macd_hist is not None and macd_hist > 0 and macd_hist < 5:
        risk_score += 10
        factors.append('MACD柱收缩，动能减弱')
    elif macd_hist is not None and macd_hist < 0:
        risk_score += 15
        factors.append('MACD转负，看多动能消退')

    if k1h_klines and len(k1h_klines.get('highs',[])) >= 3:
        h = k1h_klines['highs']; l = k1h_klines['lows']; c = k1h_klines['closes']
        for i in range(-3, 0):
            candle_range = h[i] - l[i]
            if candle_range > 0:
                upper_shadow = h[i] - max(c[i], c[i-1])
                if upper_shadow / candle_range > 0.5:
                    risk_score += 10
                    factors.append('K线出现长上影，上方卖压重')
                    break

    risk_score = max(0, min(100, risk_score))
    if risk_score >= 65:   level = '高'
    elif risk_score >= 45: level = '中高'
    elif risk_score >= 25: level = '中'
    elif risk_score >= 10: level = '中低'
    else:                  level = '低'

    reason = '；'.join(factors) if factors else '无明显假突破风险'
    return {'level': level, 'score': risk_score, 'reason': reason, 'factors': factors}


def calc_signal_confidence(tf_signal, vr, rr1, rsi_1h, macd_hist, fake_breakout, resistance, support, price):
    conf_score = 50
    reasons = []

    mtf_status = tf_signal.get('status', '无共振')
    if '强多头' in mtf_status or '强空头' in mtf_status:
        conf_score += 20; reasons.append('多周期强共振')
    elif '弱多头' in mtf_status or '弱空头' in mtf_status:
        conf_score += 8;  reasons.append('多周期弱共振')
    else:
        conf_score -= 15; reasons.append('多周期无共振')

    if vr is not None:
        if vr >= 1.5:   conf_score += 12; reasons.append('成交量放大确认')
        elif vr >= 0.8: conf_score += 4
        elif vr < 0.5:  conf_score -= 12; reasons.append('成交量严重不足')
        elif vr < 0.8:  conf_score -= 6;  reasons.append('成交量偏低')

    if rr1 >= 2.5:   conf_score += 10; reasons.append('盈亏比优秀')
    elif rr1 >= 1.5: conf_score += 5
    elif rr1 < 1.0:  conf_score -= 10; reasons.append('盈亏比不足')
    elif rr1 < 1.5:  conf_score -= 5

    if rsi_1h:
        if 50 < rsi_1h < 68:   conf_score += 8;  reasons.append('RSI健康偏多')
        elif 32 < rsi_1h < 50: conf_score -= 5
        elif rsi_1h > 72:      conf_score -= 8;  reasons.append('RSI超买风险')
        elif rsi_1h < 28:      conf_score -= 8;  reasons.append('RSI超卖')
    if macd_hist is not None:
        if macd_hist > 0:  conf_score += 5
        else:              conf_score -= 5

    if resistance and resistance[0]['dist'] < 1.0:
        conf_score -= 8; reasons.append('紧贴压力位')
    elif support and abs(support[0]['dist']) < 1.0:
        conf_score += 5; reasons.append('价格在支撑附近')

    fb_level = fake_breakout.get('level', '低')
    if fb_level == '高':      conf_score -= 15; reasons.append('假突破风险高')
    elif fb_level == '中高':  conf_score -= 10; reasons.append('假突破风险中高')
    elif fb_level == '中':    conf_score -= 5
    elif fb_level == '低':    conf_score += 5

    conf_score = max(0, min(100, conf_score))
    if conf_score >= 78:   level = '高'
    elif conf_score >= 65: level = '中高'
    elif conf_score >= 48: level = '中'
    elif conf_score >= 32: level = '中低'
    else:                  level = '低'

    reason = '；'.join(reasons) if reasons else '综合评估'
    return {'level': level, 'score': conf_score, 'reason': reason}


def calc_risk_level(scores, tp_sl, vr, tf_signal, fake_breakout, price, resistance, support, change_24h):
    risk_score = 0
    factors = []
    rr_result = tp_sl.get('risk_reward_result', {})
    rr1       = rr_result.get('rr_1', 0)
    signal    = scores.get('signal', 50)

    if abs(change_24h) > 10:
        risk_score += 20; factors.append('24H波动超10%，高波动风险')
    elif abs(change_24h) > 6:
        risk_score += 12; factors.append('24H波动较大(' + str(round(abs(change_24h),1)) + '%)')
    elif abs(change_24h) > 4:
        risk_score += 6

    if signal >= 70 and (vr is None or vr < 0.6):
        risk_score += 18; factors.append('信号偏强但成交量不支撑，追高风险')
    elif signal <= 30 and (vr is None or vr < 0.6):
        risk_score += 15; factors.append('信号偏空但成交量不支撑，追空风险')

    fb_score = fake_breakout.get('score', 0)
    if fb_score >= 65:   risk_score += 20; factors.append('假突破风险高')
    elif fb_score >= 45: risk_score += 12; factors.append('假突破风险中高')
    elif fb_score >= 25: risk_score += 6

    if vr is not None:
        if vr < 0.3:   risk_score += 18; factors.append('量比极低(' + str(vr) + 'x)，量能严重不足')
        elif vr < 0.5: risk_score += 10; factors.append('量能不足')

    mtf_status = tf_signal.get('status', '')
    if mtf_status == '无共振':
        risk_score += 15; factors.append('多周期未共振，方向不明')
    elif '弱' in mtf_status:
        risk_score += 8

    if rr1 < 1.0:   risk_score += 18; factors.append('盈亏比<1，风险大于收益')
    elif rr1 < 1.5: risk_score += 10; factors.append('盈亏比偏低(' + str(rr1) + ')')

    if resistance and resistance[0]['dist'] < 1.0:
        risk_score += 12; factors.append('紧贴压力位，上行空间有限')
    elif resistance and resistance[0]['dist'] < 2.0:
        risk_score += 6

    if support and abs(support[0]['dist']) < 0.5:
        risk_score += 15; factors.append('价格紧贴支撑，破位风险')

    risk_score = max(0, min(100, risk_score))
    if risk_score >= 70:   level = '高'
    elif risk_score >= 52: level = '中高'
    elif risk_score >= 35: level = '中'
    elif risk_score >= 18: level = '中低'
    else:                  level = '低'

    return {'level': level, 'score': risk_score, 'risk_factors': factors}


def calc_position_sizing(grade, tf_signal, vr, rr1, confidence, risk_level_val, fake_breakout_level):
    reasons = []

    if grade in ['C', 'D', 'E']:
        return {'suggested_position':'0%','level':'不操作','reason':'操作等级' + grade + '，禁止开仓'}

    if rr1 < 1.5:
        return {'suggested_position':'0%','level':'不操作','reason':'盈亏比' + str(rr1) + '<1.5，禁止开仓'}

    pos_cap = 50

    if vr is not None and vr < 0.5:
        pos_cap = min(pos_cap, 20)
        reasons.append('成交量不足限20%')

    mtf_status = tf_signal.get('status', '')
    if mtf_status == '无共振':
        pos_cap = min(pos_cap, 20)
        reasons.append('多周期无共振限20%')
    elif '弱' in mtf_status:
        pos_cap = min(pos_cap, 30)
        reasons.append('弱共振限30%')

    if fake_breakout_level == '高':
        pos_cap = min(pos_cap, 20)
        reasons.append('假突破风险高限20%')
    elif fake_breakout_level == '中高':
        pos_cap = min(pos_cap, 25)

    if risk_level_val in ['高','中高']:
        pos_cap = min(pos_cap, 20)
        reasons.append('风险等级' + risk_level_val + '限20%')

    conf_level = confidence.get('level', '中')
    if rr1 >= 2.5 and conf_level in ['高','中高'] and '强' in mtf_status:
        base_pos = 50; level_text = '标准仓位'; reasons.append('强趋势共振+高盈亏比')
    elif rr1 >= 2.0 and conf_level in ['高','中高']:
        base_pos = 30; level_text = '中等仓位'; reasons.append('盈亏比优秀+置信度高')
    elif rr1 >= 1.5 and conf_level in ['中','中高']:
        base_pos = 20; level_text = '小仓位试单'; reasons.append('盈亏比合理')
    else:
        base_pos = 10; level_text = '轻仓试探'; reasons.append('置信度偏低，轻仓')

    final_pos = min(base_pos, pos_cap)

    if final_pos == 0:
        pos_str = '0%'; level_text = '不操作'
    elif final_pos <= 10:
        pos_str = '5%-10%'; level_text = '轻仓试探'
    elif final_pos <= 20:
        pos_str = '10%-20%'; level_text = '小仓位试单'
    elif final_pos <= 30:
        pos_str = '20%-30%'; level_text = '中等仓位'
    else:
        pos_str = '30%-50%'; level_text = '标准仓位'

    return {
        'suggested_position': pos_str,
        'level': level_text,
        'reason': '；'.join(reasons) if reasons else '综合评估',
    }
def full_analysis(coin):
    cfg = COIN_CONFIG[coin]
    symbol = cfg['symbol']
    ticker = get_ticker(coin)
    if not ticker:
        print(f'[ERROR] ticker_fetch_failed: {coin}')
        return {'_error': 'ticker_fetch_failed', '_detail': f'无法获取{coin}价格，Binance和CoinGecko均失败'}
    price = ticker['price']
    result = {'coin': coin, 'name': cfg['name'], 'price': price,
              'change_24h': ticker['change'], 'high_24h': ticker['high'],
              'low_24h': ticker['low'], 'quote_volume_24h': round(ticker['quoteVolume']/1e6, 2)}
    klines = {}
    for iv, lim in [('15m',100),('1h',200),('4h',150),('1d',60)]:
        klines[iv] = get_klines(symbol, iv, lim)
        time.sleep(0.2)
    k15=klines.get('15m'); k1h=klines.get('1h'); k4h=klines.get('4h'); k1d=klines.get('1d')
    if not k1h:
        print(f'[ERROR] klines_fetch_failed: {coin} 1h')
        return {'_error': 'klines_fetch_failed', '_detail': f'{coin} 1小时K线获取失败，Binance全部节点不可用'}
    if k1h:
        c = k1h['closes']
        result['ma5_1h']=ma(c,5); result['ma10_1h']=ma(c,10)
        result['ma20_1h']=ma(c,20); result['ma50_1h']=ma(c,50)
        result['ema20_1h']=ema(c,20)
        result['rsi_1h']=rsi(c)
        m,s,h=macd(c); result['macd_1h']=m; result['macd_signal_1h']=s; result['macd_hist_1h']=h
        bl,bm,bu=bollinger(c); result['bb_lower_1h']=bl; result['bb_mid_1h']=bm; result['bb_upper_1h']=bu
        result['atr_1h']=atr(k1h['highs'],k1h['lows'],c)
        result['vol_ratio_1h']=vol_ratio(k1h['volumes'])
    if k4h:
        c=k4h['closes']; result['ma20_4h']=ma(c,20); result['rsi_4h']=rsi(c)
    if k1d:
        c=k1d['closes']; result['ma50_1d']=ma(c,50); result['rsi_1d']=rsi(c)
    result['timeframe_trends']=[
        trend_by_tf(k15,'15分钟'), trend_by_tf(k1h,'1小时'),
        trend_by_tf(k4h,'4小时'), trend_by_tf(k1d,'日线')]
    va=volume_analysis(k1h)
    result['vol_ratio_1h']=va.get('vol_ratio')
    result['volume_signal']=va.get('signal','')
    result['volume_signal_detail']=va.get('signal_detail','')
    result['volume_warning']=va.get('volume_warning','')
    sr=support_resistance(k1h,k4h,k1d,price)
    result['resistance']=sr['resistance']; result['support']=sr['support']
    result['resistance_levels']=[r['price'] for r in sr['resistance']]
    result['support_levels']=[s['price'] for s in sr['support']]
    if k1h:
        result['intraday_high']=round(max(k1h['highs'][-24:]),4)
        result['intraday_low']=round(min(k1h['lows'][-24:]),4)
    if k1d:
        result['week_high']=round(max(k1d['highs'][-7:]),4)
        result['week_low']=round(min(k1d['lows'][-7:]),4)
    result['predicted_range']=predict_range(k1h,price,result.get('atr_1h'))
    result['scores']=calc_scores(result)
    result['tp_sl']=calc_tp_sl(price,result.get('atr_1h'),result['scores']['signal'],sr)
    vr = result.get('vol_ratio_1h')
    result['final_action_grade']=calc_final_action_grade(result['scores'],result['tp_sl'],vr,result['timeframe_trends'])

    tf_list   = result['timeframe_trends']
    rr1       = result['tp_sl'].get('risk_reward_result',{}).get('rr_1', 0)
    rsi_1h    = result.get('rsi_1h')
    macd_h    = result.get('macd_hist_1h')
    resistance= result.get('resistance', [])
    support_l = result.get('support', [])

    result['multi_timeframe_signal'] = calc_multi_timeframe_signal(tf_list, vr)
    result['market_regime']          = calc_market_regime(
        tf_list, vr, result.get('volume_signal',''),
        price, resistance, support_l, rsi_1h, result.get('change_24h',0))
    result['fake_breakout_risk']     = calc_fake_breakout_risk(
        price, resistance, support_l, vr, rsi_1h, tf_list, macd_h, k1h)
    result['signal_confidence']      = calc_signal_confidence(
        result['multi_timeframe_signal'], vr, rr1, rsi_1h, macd_h,
        result['fake_breakout_risk'], resistance, support_l, price)
    result['risk_level']             = calc_risk_level(
        result['scores'], result['tp_sl'], vr,
        result['multi_timeframe_signal'], result['fake_breakout_risk'],
        price, resistance, support_l, result.get('change_24h',0))

    fb_level = result['fake_breakout_risk']['level']
    if fb_level == '高':
        result['scores']['signal']    = min(result['scores']['signal'], 65)
        result['final_score']         = result['scores']['signal']
        grade_order = ['E','D','C','B','A','A+']
        cur = result['final_action_grade']['grade']
        if grade_order.index(cur) > grade_order.index('B'):
            result['final_action_grade']['grade']  = 'B'
            result['final_action_grade']['action'] = '小仓位试单（假突破风险高，等级压制至B）'
            result['final_action_grade']['reason'] += ' | 假突破风险高限制等级'

    result['position_sizing'] = calc_position_sizing(
        result['final_action_grade']['grade'],
        result['multi_timeframe_signal'], vr, rr1,
        result['signal_confidence'],
        result['risk_level']['level'],
        fb_level)

    result['final_score']      = result['scores']['signal']
    result['signal_direction'] = result['scores'].get('signal_direction','震荡')
    result['volume_warning']   = result.get('volume_warning','')
    result['risk_reward']      = result['tp_sl'].get('risk_reward_result',{})
    result['updated_at']       = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return result


def _fmt_price(p):
    if p is None: return '--'
    p = float(p)
    if p >= 1000:   return str(round(p, 2))
    if p >= 1:      return str(round(p, 4))
    return str(round(p, 6))


def build_structured_report(data):
    coin    = data.get('coin', '--')
    price   = data.get('price', 0)
    scores  = data.get('scores', {})
    tp_sl   = data.get('tp_sl', {})
    rr      = tp_sl.get('risk_reward_result', {})
    ag      = data.get('final_action_grade', {})
    mr      = data.get('market_regime', {})
    mtf     = data.get('multi_timeframe_signal', {})
    fb      = data.get('fake_breakout_risk', {})
    conf    = data.get('signal_confidence', {})
    rl      = data.get('risk_level', {})
    ps      = data.get('position_sizing', {})
    tf_list = data.get('timeframe_trends', [])
    sr      = data.get('resistance', [])
    ss      = data.get('support', [])
    vr      = data.get('vol_ratio_1h')
    grade   = ag.get('grade', '--')
    signal  = scores.get('signal', '--')
    conf_lv = conf.get('level', '--')

    L = []

    L.append('【综合判断】')
    L.append(coin + '/USDT  当前价 $' + _fmt_price(price) +
             '  24H ' + ('+' if data.get('change_24h',0)>=0 else '') +
             str(round(data.get('change_24h',0),2)) + '%')
    L.append('综合信号：' + str(scores.get('signal_text','--')) +
             '（' + str(signal) + '/100，方向：' + str(scores.get('signal_direction','--')) + '）')
    L.append('信号置信度：' + conf_lv +
             '  操作等级：' + grade + ' — ' + ag.get('action','--'))
    if scores.get('vol_cap_reason'):
        L.append('⚠️ ' + scores['vol_cap_reason'])

    L.append('')
    L.append('【市场状态】')
    L.append(mr.get('type','--') + '（置信度：' + mr.get('confidence','--') + '）')
    L.append(mr.get('reason',''))
    if mtf.get('status'):
        L.append('多周期共振：' + mtf['status'] + '（共振分 ' + str(mtf.get('score','--')) + '）')
        L.append(mtf.get('note',''))

    L.append('')
    L.append('【多周期趋势】')
    tf_map = {t['label']: t for t in tf_list}
    for label in ['15分钟','1小时','4小时','日线']:
        t = tf_map.get(label, {})
        if t:
            L.append(label + '：' + t.get('trend','--') + '（' + str(t.get('score','--')) + '/100）')
        else:
            L.append(label + '：数据不足')

    L.append('')
    L.append('【成交量】')
    vr_str = str(round(vr,2)) + 'x' if vr is not None else '--'
    L.append('量比：' + vr_str + '  ' + data.get('volume_signal','--'))
    L.append(data.get('volume_signal_detail',''))
    vol_warn = data.get('volume_warning','')
    if vol_warn:
        L.append('⚠️ ' + vol_warn)
    if vr is not None and vr < 0.8:
        L.append('⚠️ 成交量不足，不能视为强趋势信号')

    L.append('')
    L.append('【关键价位】')
    near_res   = [r for r in sr if r.get('dist',99) < 3.0][:1]
    strong_res = [r for r in sr if r.get('dist',99) >= 3.0][:1]
    near_sup   = [s for s in ss if abs(s.get('dist',99)) < 3.0][:1]
    strong_sup = [s for s in ss if abs(s.get('dist',99)) >= 3.0][:1]
    if not near_res and sr:   near_res   = [sr[0]]
    if not strong_res and len(sr)>1: strong_res = [sr[1]]
    if not near_sup and ss:   near_sup   = [ss[0]]
    if not strong_sup and len(ss)>1: strong_sup = [ss[1]]

    L.append('近端压力：' + ('$' + _fmt_price(near_res[0]['price']) + ' (距' + str(near_res[0]['dist']) + '%)' if near_res else '暂无'))
    L.append('强压力：'   + ('$' + _fmt_price(strong_res[0]['price']) if strong_res else '暂无'))
    L.append('近端支撑：' + ('$' + _fmt_price(near_sup[0]['price']) + ' (距' + str(near_sup[0]['dist']) + '%)' if near_sup else '暂无'))
    L.append('强支撑：'   + ('$' + _fmt_price(strong_sup[0]['price']) if strong_sup else '暂无'))
    if near_res:
        confirm = round(near_res[0]['price'] * 1.005, 4)
        L.append('突破确认位：$' + _fmt_price(confirm) + '（收盘站上视为有效突破）')
    if near_sup:
        invalidate = round(near_sup[0]['price'] * 0.995, 4)
        L.append('跌破失效位：$' + _fmt_price(invalidate) + '（跌破视为支撑失守）')

    L.append('')
    L.append('【入场条件】')
    valid_trade = rr.get('valid_trade', False)
    rr1 = rr.get('rr_1', 0)
    mtf_status = mtf.get('status','')
    direction = tp_sl.get('direction','long')

    if not valid_trade or grade in ['C','D','E']:
        L.append('当前不建议直接开仓。')
        L.append('激进入场：不适用（' + rr.get('note','盈亏比不足') + '）')
        L.append('稳健入场：不适用')
        L.append('回踩入场：等待价格回踩支撑 $' + (_fmt_price(near_sup[0]['price']) if near_sup else '--') + ' 附近确认后再考虑')
        L.append('无效条件：当前盈亏比或信号不满足开仓条件')
    else:
        entry_price = _fmt_price(price)
        if direction == 'long':
            L.append('激进入场：当前价 $' + entry_price + ' 直接做多，止损见下')
            if near_sup:
                L.append('稳健入场：等待回踩 $' + _fmt_price(near_sup[0]['price']) + ' 附近确认支撑后进场')
            else:
                L.append('稳健入场：等待回调整固后进场')
            L.append('回踩入场：回踩至支撑区 $' + (_fmt_price(near_sup[0]['price']) if near_sup else '--') + ' 量能未萎缩时进场')
        else:
            L.append('激进入场：当前价 $' + entry_price + ' 直接做空，止损见下')
            if near_res:
                L.append('稳健入场：等待反弹至 $' + _fmt_price(near_res[0]['price']) + ' 附近确认压力后进场')
            else:
                L.append('稳健入场：等待反弹整固后进场')
            L.append('回踩入场：反弹至压力区 $' + (_fmt_price(near_res[0]['price']) if near_res else '--') + ' 量能萎缩时进场')
        L.append('无效条件：' + ('成交量极低时不入场' if vr and vr < 0.5 else '信号消失或止损先触发则取消'))

    L.append('')
    L.append('【止盈止损】')
    L.append('计划入场：$' + _fmt_price(rr.get('entry', price)))
    sl_tag  = '（多头，跌破止损）' if direction=='long' else '（空头，突破止损）'
    tp1_tag = '（多头，第一目标）' if direction=='long' else '（空头，第一目标）'
    tp2_tag = '（多头，第二目标）' if direction=='long' else '（空头，第二目标）'
    L.append('止损：$' + _fmt_price(rr.get('stop_loss', tp_sl.get('stop_loss',0))) +
             '（-' + str(rr.get('sl_pct', tp_sl.get('sl_pct',0))) + '%）' + sl_tag)
    L.append('止盈1：$' + _fmt_price(rr.get('take_profit_1', tp_sl.get('take_profit_1',0))) +
             '（+' + str(rr.get('tp1_pct', tp_sl.get('tp1_pct',0))) + '%）' + tp1_tag)
    L.append('止盈2：$' + _fmt_price(rr.get('take_profit_2', tp_sl.get('take_profit_2',0))) +
             '（+' + str(rr.get('tp2_pct', tp_sl.get('tp2_pct',0))) + '%）' + tp2_tag)
    L.append('第一目标盈亏比：' + str(rr.get('rr_1', tp_sl.get('risk_reward',0))) +
             '   第二目标盈亏比：' + str(rr.get('rr_2','--')))
    rr_note_raw = rr.get('note', '--')
    pos_zero = ps.get('suggested_position', '0%') == '0%'
    suppress = (grade in ['D','E'] or pos_zero or rl.get('level','') in ['高','中高'])
    if suppress and '优质机会' in rr_note_raw:
        L.append('盈亏比理论上较高，但当前量能不足、风险过高，不构成可执行交易机会。')
    else:
        L.append(rr_note_raw)

    L.append('')
    L.append('【仓位建议】')
    L.append('建议仓位：' + ps.get('suggested_position','0%'))
    L.append('仓位等级：' + ps.get('level','--'))
    L.append('原因：' + ps.get('reason','--'))

    L.append('')
    L.append('【风险提示】')
    risk_factors = rl.get('risk_factors', [])
    fb_factors   = fb.get('factors', [])
    all_risks = []
    seen = set()
    for f in (risk_factors + fb_factors):
        key = f[:10]
        if key not in seen:
            seen.add(key); all_risks.append(f)
    if all_risks:
        for f in all_risks[:6]:
            L.append('• ' + f)
    else:
        L.append('• 当前无明显异常风险')
    L.append('风险等级：' + rl.get('level','--') + '（风险分 ' + str(rl.get('score','--')) + '/100）')
    if fb.get('level') in ['高','中高']:
        L.append('⚠️ 假突破风险：' + fb.get('level','') + ' — ' + fb.get('reason',''))

    L.append('')
    L.append('【最终建议】')
    grade_conclusion = {
        'A+': '当前操作等级 A+，高质量机会，可按建议仓位开仓，严格执行止损。',
        'A':  '当前操作等级 A，可交易，建议按计划执行，注意止损纪律。',
        'B':  '当前操作等级 B，建议小仓位试单，止损要严，不要加仓追高。',
        'C':  '当前操作等级 C，建议观望，等待更好的入场机会再考虑。',
        'D':  '当前操作等级 D，风险过高，不建议交易，等待风险因素消除。',
        'E':  '当前操作等级 E，禁止交易，止盈止损逻辑异常或盈亏比严重不合理。',
    }
    conclusion = grade_conclusion.get(grade, '当前操作等级 ' + grade + '，请参考以上分析决策。')
    if grade in ['C','D','E']:
        if vr is not None and vr < 0.8:
            conclusion += '等待成交量放大确认方向。'
        elif near_sup:
            conclusion += '可关注支撑 $' + _fmt_price(near_sup[0]['price']) + ' 是否有效。'
    L.append(conclusion)

    return '\n'.join(L)


def build_telegram_message(data):
    coin   = data.get('coin','--')
    price  = data.get('price', 0)
    scores = data.get('scores', {})
    tp_sl  = data.get('tp_sl', {})
    rr     = tp_sl.get('risk_reward_result', {})
    ag     = data.get('final_action_grade', {})
    mr     = data.get('market_regime', {})
    conf   = data.get('signal_confidence', {})
    rl     = data.get('risk_level', {})
    ps     = data.get('position_sizing', {})
    sr     = data.get('resistance', [])
    ss     = data.get('support', [])
    vr     = data.get('vol_ratio_1h')
    grade  = ag.get('grade','--')

    grade_emoji = {'A+':'🟢','A':'🟢','B':'🟡','C':'⚪','D':'🔴','E':'⛔'}
    emoji = grade_emoji.get(grade,'📊')

    lines = []
    lines.append(emoji + ' <b>' + coin + '/USDT 短线提醒</b>')
    lines.append('')
    lines.append('价格：$' + _fmt_price(price) +
                 '  (' + ('+' if data.get('change_24h',0)>=0 else '') +
                 str(round(data.get('change_24h',0),2)) + '%)')
    lines.append('状态：' + mr.get('type','--'))
    lines.append('信号：' + str(scores.get('signal','--')) + '/100  ' + str(scores.get('signal_text','--')))
    lines.append('置信度：' + conf.get('level','--') +
                 '   操作等级：<b>' + grade + '</b> ' + ag.get('action','').split('，')[0])
    lines.append('')
    res_str = ' / '.join(['$' + _fmt_price(r['price']) for r in sr[:2]]) or '暂无'
    sup_str = ' / '.join(['$' + _fmt_price(s['price']) for s in ss[:2]]) or '暂无'
    lines.append('压力：' + res_str)
    lines.append('支撑：' + sup_str)
    lines.append('')
    lines.append('止损：$' + _fmt_price(rr.get('stop_loss', tp_sl.get('stop_loss',0))) +
                 '  止盈1：$' + _fmt_price(rr.get('take_profit_1', tp_sl.get('take_profit_1',0))))
    lines.append('盈亏比：TP1 ' + str(rr.get('rr_1','--')) +
                 '，TP2 ' + str(rr.get('rr_2','--')))
    lines.append('仓位建议：' + ps.get('suggested_position','0%') + ' — ' + ps.get('level','--'))
    risk_factors = rl.get('risk_factors',[])[:3]
    if risk_factors:
        lines.append('')
        lines.append('风险：' + '、'.join([f[:10] for f in risk_factors]))
    lines.append('')
    grade_conclusion_short = {
        'A+': '高质量机会，可按计划开仓，严守止损。',
        'A':  '可交易，按计划执行，注意止损纪律。',
        'B':  '建议小仓位试单，止损要严，不追高。',
        'C':  '建议观望，等待更好入场机会。',
        'D':  '风险过高，不建议交易。',
        'E':  '禁止交易，止盈止损异常。',
    }
    lines.append('建议：' + grade_conclusion_short.get(grade, '参考以上分析决策。'))
    return '\n'.join(lines)


def should_send_telegram(data):
    grade  = data.get('final_action_grade',{}).get('grade','E')
    rr     = data.get('tp_sl',{}).get('risk_reward_result',{})
    rr1    = rr.get('rr_1', 0)
    conf   = data.get('signal_confidence',{}).get('level','低')
    fb     = data.get('fake_breakout_risk',{}).get('level','低')
    rl     = data.get('risk_level',{}).get('level','低')
    vol_sig= data.get('volume_signal','')
    vr     = data.get('vol_ratio_1h')
    mr_type= data.get('market_regime',{}).get('type','')
    scores_map = {t['label']: t['score'] for t in data.get('timeframe_trends',[])}
    s15 = scores_map.get('15分钟',50); s1h = scores_map.get('1小时',50)

    if grade in ['A+','A','B'] and rr1 >= 1.5 and conf in ['高','中高','中']:
        return True, '交易机会'
    if fb == '高' or rl == '高' or '放量下跌' in vol_sig:
        return True, '风险提醒'
    if fb in ['高','中高'] and mr_type == '假突破嫌疑':
        return True, '假突破风险'
    if (mr_type in ['放量突破','趋势上涨'] and
        vr is not None and vr >= 1.2 and
        s15 >= 60 and s1h >= 60):
        return True, '放量突破'
    if (mr_type in ['破位下跌','趋势下跌'] and
        vr is not None and vr >= 1.0 and
        s15 <= 40 and s1h <= 40):
        return True, '破位下跌'
    return False, ''


def generate_report(data):
    if not data: return '数据获取失败'
    if ANTHROPIC_API_KEY:
        try:
            structured = build_structured_report(data)
            prompt = ('你是专业加密货币短线交易分析师。\n'
                      '以下是系统生成的结构化分析报告，请在不改变任何数字、方向结论和操作等级的前提下，'
                      '优化措辞使其更专业流畅。\n'
                      '严格禁止：改变操作等级、改变盈亏比数字、改变止盈止损价格、自行添加不在报告中的数字。\n\n'
                      + structured)
            resp = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={'x-api-key': ANTHROPIC_API_KEY,
                         'anthropic-version': '2023-06-01',
                         'content-type': 'application/json'},
                json={'model': 'claude-sonnet-4-20250514', 'max_tokens': 1200,
                      'messages': [{'role': 'user', 'content': prompt}]},
                timeout=30)
            return resp.json()['content'][0]['text']
        except: pass
    return build_structured_report(data)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        r = requests.post('https://api.telegram.org/bot' + TELEGRAM_TOKEN + '/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}, timeout=10)
        return r.status_code == 200
    except: return False


def check_alerts():
    while True:
        try:
            for coin in COIN_CONFIG:
                t = get_ticker(coin)
                if t: price_cache[coin] = t['price']
                time.sleep(1)
            price_cache['last_update'] = datetime.now().strftime('%H:%M:%S')
            alerts = load_json(ALERTS_FILE, [])
            history = load_json(HISTORY_FILE, [])
            changed = False
            for alert in alerts:
                if alert.get('triggered'): continue
                coin = alert['coin']
                current = price_cache.get(coin, 0)
                target = float(alert['price'])
                cond = alert['condition']
                if (cond=='above' and current>target) or (cond=='below' and current<target):
                    alert['triggered'] = True; changed = True
                    sym = '📈' if cond=='above' else '📉'
                    send_telegram(sym + ' <b>价格提醒触发！</b>\n币种：' + coin + '/USDT\n条件：' + ('高于' if cond=='above' else '低于') + ' $' + str(target) + '\n当前：$' + str(current) + '\n时间：' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    history.insert(0, {'coin':coin,'condition':cond,'target':target,'price':current,'time':datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                    history = history[:50]
            if changed: save_json(ALERTS_FILE, alerts); save_json(HISTORY_FILE, history)
        except Exception as e: print('Monitor error: ' + str(e))
        time.sleep(30)


@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/prices')
def api_prices(): return jsonify(price_cache)

@app.route('/api/coins')
def api_coins(): return jsonify(list(COIN_CONFIG.keys()))

@app.route('/api/report/<coin>')
def api_report(coin):
    coin = coin.upper()
    if coin not in COIN_CONFIG:
        return jsonify({'error': 'invalid_coin', 'detail': f'不支持的币种: {coin}'})
    try:
        data = full_analysis(coin)
    except Exception as e:
        print(f'[ERROR] full_analysis exception: {coin} {e}')
        return jsonify({'error': 'analysis_exception', 'detail': str(e)})
    if not data:
        return jsonify({'error': 'fetch_failed', 'detail': '数据获取失败，请稍后重试'})
    if '_error' in data:
        print(f'[ERROR] {data["_error"]}: {data.get("_detail","")}')
        return jsonify({'error': data['_error'], 'detail': data['_detail']})
    required = ['scores', 'tp_sl', 'timeframe_trends', 'final_action_grade']
    missing = [k for k in required if k not in data]
    if missing:
        print(f'[ERROR] analysis_field_missing: {missing}')
        return jsonify({'error': 'analysis_field_missing', 'detail': f'缺少字段: {missing}'})
    try:
        data['report'] = generate_report(data)
    except Exception as e:
        print(f'[ERROR] report_build_failed: {e}')
        data['report'] = f'报告生成失败：{e}'
    should_send, tg_reason = should_send_telegram(data)
    if should_send:
        try:
            tg_msg = build_telegram_message(data)
            send_telegram(tg_msg)
        except Exception as e:
            print(f'[WARN] telegram_failed: {e}')
    return jsonify(data)

@app.route('/api/alerts', methods=['GET'])
def get_alerts(): return jsonify(load_json(ALERTS_FILE, []))

@app.route('/api/alerts', methods=['POST'])
def add_alert():
    d = request.json
    alerts = load_json(ALERTS_FILE, [])
    alerts.append({'id':int(time.time()*1000),'coin':d['coin'],'condition':d['condition'],
                   'price':float(d['price']),'triggered':False,
                   'created':datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    save_json(ALERTS_FILE, alerts)
    return jsonify({'ok': True})

@app.route('/api/alerts/<int:aid>', methods=['DELETE'])
def del_alert(aid):
    save_json(ALERTS_FILE, [a for a in load_json(ALERTS_FILE,[]) if a['id']!=aid])
    return jsonify({'ok': True})

@app.route('/api/history')
def get_history(): return jsonify(load_json(HISTORY_FILE, []))

threading.Thread(target=check_alerts, daemon=True).start()
