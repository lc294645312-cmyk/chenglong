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

# ─── 工具 ────────────────────────────────────────────────────

def load_json(f, d):
    try:
        if os.path.exists(f):
            with open(f) as fp: return json.load(fp)
    except: pass
    return d

def save_json(f, d):
    with open(f, 'w') as fp: json.dump(d, fp, ensure_ascii=False, indent=2)

# ─── 数据获取 ────────────────────────────────────────────────

def get_ticker(coin):
    cfg = COIN_CONFIG[coin]
    cg_id = cfg['cg_id']
    try:
        url = f'https://api.coingecko.com/api/v3/coins/{cg_id}?localization=false&tickers=false&community_data=false&developer_data=false'
        r = requests.get(url, timeout=15)
        d = r.json()['market_data']
        return {
            'price': d['current_price']['usd'],
            'change': d['price_change_percentage_24h'] or 0,
            'high': d['high_24h']['usd'],
            'low': d['low_24h']['usd'],
            'volume': d['total_volume']['usd'] / max(d['current_price']['usd'], 0.0001),
            'quoteVolume': d['total_volume']['usd'],
        }
    except:
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
            r = requests.get(url, timeout=10)
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
# ─── 技术指标 ────────────────────────────────────────────────

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
    r = closes[-n:]
    mid = sum(r) / n
    std = (sum((x-mid)**2 for x in r) / n) ** 0.5
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

# ─── 多周期趋势 ──────────────────────────────────────────────

def trend_by_tf(klines, label):
    if not klines: return {'label': label, 'trend': '数据不足', 'score': 50}
    c = klines['closes']
    price = c[-1]
    m20 = ma(c, 20)
    m50 = ma(c, 50)
    r = rsi(c)
    mc, sig, hist = macd(c)
    score = 50
    details = []
    if m20 and price > m20: score += 10; details.append('价格在MA20上方')
    elif m20: score -= 10; details.append('价格在MA20下方')
    if m50 and price > m50: score += 8; details.append('价格在MA50上方')
    elif m50: score -= 8; details.append('价格在MA50下方')
    if m20 and m50 and m20 > m50: score += 7; details.append('MA20>MA50金叉')
    elif m20 and m50: score -= 7; details.append('MA20<MA50死叉')
    if r:
        if r > 60: score += 10; details.append(f'RSI={r:.0f}偏强')
        elif r < 40: score -= 10; details.append(f'RSI={r:.0f}偏弱')
        else: details.append(f'RSI={r:.0f}中性')
    if mc and mc > 0: score += 8; details.append('MACD>0看多')
    elif mc: score -= 8; details.append('MACD<0看空')
    score = max(0, min(100, score))
    if score >= 65: trend = '上涨 📈'
    elif score <= 35: trend = '下跌 📉'
    else: trend = '震荡 ↔️'
    return {'label': label, 'trend': trend, 'score': score, 'details': details}

# ─── 放量突破识别 ────────────────────────────────────────────

def volume_analysis(klines):
    if not klines: return {}
    vols = klines['volumes']
    closes = klines['closes']
    highs = klines['highs']
    vr = vol_ratio(vols)
    result = {'vol_ratio': vr}
    if not vr: return result
    price_change = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
    recent_high = max(highs[-20:]) if len(highs) >= 20 else highs[-1]
    if vr >= 2.0 and price_change > 0.5:
        result['signal'] = '🚀 放量突破'
        result['signal_detail'] = f'成交量是均量{vr:.1f}倍，价格上涨{price_change:.1f}%，突破信号强烈'
    elif vr >= 1.5 and price_change > 0:
        result['signal'] = '📈 温和放量上涨'
        result['signal_detail'] = f'量比{vr:.1f}x，温和上涨，可关注持续性'
    elif vr >= 2.0 and price_change < -0.5:
        result['signal'] = '🔻 放量下跌'
        result['signal_detail'] = f'成交量{vr:.1f}倍，价格下跌{price_change:.1f}%，下行压力大'
    elif vr < 0.5:
        result['signal'] = '😴 缩量横盘'
        result['signal_detail'] = f'量比仅{vr:.1f}x，成交清淡，方向待定'
    else:
        result['signal'] = '➡️ 正常波动'
        result['signal_detail'] = f'量比{vr:.1f}x，成交量正常'
    return result

# ─── 支撑压力识别 ────────────────────────────────────────────

def support_resistance(k1h, k4h, k1d, price):
    res, sup = [], []
    labels_res, labels_sup = {}, {}

    def add_level(val, is_res, label):
        if is_res and val > price:
            res.append(val)
            labels_res[round(val, 8)] = label
        elif not is_res and val < price:
            sup.append(val)
            labels_sup[round(val, 8)] = label

    for klines, tf in [(k1h, '1H'), (k4h, '4H'), (k1d, '1D')]:
        if not klines: continue
        h, l, c = klines['highs'], klines['lows'], klines['closes']
        n = min(len(h), {'1H':48,'4H':30,'1D':14}[tf])
        add_level(max(h[-n:]), True, f'{tf}近期高点')
        add_level(min(l[-n:]), False, f'{tf}近期低点')
        sh, sl = swing_points(h, l)
        for v in sh: add_level(round(v,8), True, f'{tf}摆动高点')
        for v in sl: add_level(round(v,8), False, f'{tf}摆动低点')
        # MA支撑压力
        for n_ma, name in [(20,'MA20'),(50,'MA50'),(200,'MA200')]:
            v = ma(c, n_ma)
            if v: add_level(v, v > price, f'{tf}{name}')

    # 去重合并相近价位（1.5%以内合并）
    def merge_levels(levels):
        if not levels: return []
        levels = sorted(set(round(l, 6) for l in levels))
        merged = [levels[0]]
        for l in levels[1:]:
            if abs(l - merged[-1]) / merged[-1] > 0.015:
                merged.append(l)
        return merged

    res_merged = merge_levels(res)[:4]
    sup_merged = merge_levels(sorted(sup, reverse=True))[:4]

    return {
        'resistance': [{'price': r, 'label': labels_res.get(round(r,8),'压力位'), 'dist': round((r-price)/price*100,2)} for r in res_merged],
        'support':    [{'price': s, 'label': labels_sup.get(round(s,8),'支撑位'), 'dist': round((s-price)/price*100,2)} for s in sup_merged],
    }

# ─── 短线高低点预测 ──────────────────────────────────────────

def predict_range(k1h, price, atr_val):
    if not k1h or not atr_val:
        return {'high': round(price*1.02,8), 'low': round(price*0.98,8), 'basis': '基于2%估算'}
    closes = k1h['closes']
    highs = k1h['highs']
    lows = k1h['lows']
    # 用ATR * 系数预测
    pred_high = round(price + atr_val * 2.5, 8)
    pred_low  = round(price - atr_val * 2.5, 8)
    # 找最近支撑压力修正
    near_res = [h for h in highs[-48:] if h > price]
    near_sup = [l for l in lows[-48:] if l < price]
    if near_res: pred_high = min(pred_high, round(min(near_res)*1.002, 8))
    if near_sup: pred_low  = max(pred_low,  round(max(near_sup)*0.998, 8))
    rng = round((pred_high - pred_low) / price * 100, 2)
    return {
        'high': pred_high,
        'low':  pred_low,
        'range_pct': rng,
        'basis': f'ATR×2.5={round(atr_val*2.5,4)}，预测波动区间{rng}%'
    }

# ─── 评分系统 ────────────────────────────────────────────────

def calc_scores(data, klines_dict):
    price = data.get('price', 0)
    scores = {}

    # 1. Trend Score 趋势分数
    tf_scores = [t['score'] for t in data.get('timeframe_trends', []) if 'score' in t]
    trend_score = round(sum(tf_scores) / len(tf_scores)) if tf_scores else 50
    scores['trend'] = trend_score

    # 2. Momentum Score 动能分数
    rsi_1h = data.get('rsi_1h')
    rsi_4h = data.get('rsi_4h')
    macd_h = data.get('macd_hist_1h')
    mom = 50
    if rsi_1h:
        if rsi_1h > 70: mom += 20
        elif rsi_1h > 60: mom += 12
        elif rsi_1h > 50: mom += 5
        elif rsi_1h < 30: mom -= 20
        elif rsi_1h < 40: mom -= 12
        elif rsi_1h < 50: mom -= 5
    if rsi_4h:
        if rsi_4h > 60: mom += 10
        elif rsi_4h < 40: mom -= 10
    if macd_h:
        if macd_h > 0: mom += 10
        else: mom -= 10
    scores['momentum'] = max(0, min(100, round(mom)))

    # 3. Volume Score 成交量分数
    vr = data.get('vol_ratio_1h')
    vol_sig = data.get('volume_signal', '')
    vol = 50
    if vr:
        if vr >= 2.0: vol = 85 if '上涨' in vol_sig or '突破' in vol_sig else 30
        elif vr >= 1.5: vol = 70 if price > (data.get('ma20_1h') or price) else 45
        elif vr < 0.5: vol = 35
        else: vol = 55
    scores['volume'] = max(0, min(100, round(vol)))

    # 4. Risk Score 风险分数（越高越危险）
    rsi_v = rsi_1h or 50
    bb_upper = data.get('bb_upper_1h')
    bb_lower = data.get('bb_lower_1h')
    risk = 40
    if rsi_v > 75: risk += 25
    elif rsi_v > 70: risk += 15
    elif rsi_v < 25: risk += 25
    elif rsi_v < 30: risk += 15
    if bb_upper and price > bb_upper * 0.99: risk += 15
    if bb_lower and price < bb_lower * 1.01: risk += 15
    change = abs(data.get('change_24h', 0))
    if change > 8: risk += 15
    elif change > 5: risk += 8
    scores['risk'] = max(0, min(100, round(risk)))

    # 5. Signal Score 综合信号
    signal = round(
        trend_score * 0.35 +
        scores['momentum'] * 0.30 +
        scores['volume'] * 0.20 +
        (100 - scores['risk']) * 0.15
    )
    scores['signal'] = max(0, min(100, signal))

    # 信号解读
    def interpret(s):
        if s >= 80: return '强烈看多 🚀', 'bull'
        elif s >= 60: return '偏多 📈', 'bull'
        elif s >= 40: return '震荡观望 ↔️', 'neutral'
        elif s >= 20: return '偏空 📉', 'bear'
        else: return '强烈看空 🔻', 'bear'

    scores['signal_text'], scores['signal_class'] = interpret(scores['signal'])

    # 各项说明
    scores['trend_reason']    = f"多周期均线趋势综合得分，分数{'偏高说明趋势向上' if trend_score > 60 else '偏低说明趋势向下' if trend_score < 40 else '居中说明趋势不明'}"
    scores['momentum_reason'] = f"RSI={rsi_1h or '--'}，MACD柱{'正值动能强' if macd_h and macd_h > 0 else '负值动能弱' if macd_h else '计算中'}，动能{'较强' if scores['momentum']>60 else '较弱' if scores['momentum']<40 else '中性'}"
    scores['volume_reason']   = f"量比{vr or '--'}x，{data.get('volume_signal_detail','')}"
    scores['risk_reason']     = f"RSI{'超买风险' if rsi_v>70 else '超卖反弹风险' if rsi_v<30 else '正常区间'}，24H波动{abs(data.get('change_24h',0)):.1f}%，{'高波动高风险' if change>5 else '波动正常'}"
    scores['signal_reason']   = f"趋势35%+动能30%+成交量20%+风险15%加权，综合信号{scores['signal_text']}"

    return scores

# ─── 止盈止损 ────────────────────────────────────────────────

def calc_tp_sl(price, atr_val, signal_score, sr):
    if not atr_val: atr_val = price * 0.02
    res_list = sr.get('resistance', [])
    sup_list = sr.get('support', [])
    # 止损：最近支撑位下方，或ATR*1.5
    if sup_list:
        sl = round(sup_list[0]['price'] * 0.998, 8)
    else:
        sl = round(price - atr_val * 1.5, 8)
    # 止盈：分级
    if res_list:
        tp1 = round(res_list[0]['price'] * 0.998, 8)
        tp2 = round(res_list[1]['price'] * 0.998, 8) if len(res_list) > 1 else round(price + atr_val * 3, 8)
    else:
        tp1 = round(price + atr_val * 2, 8)
        tp2 = round(price + atr_val * 4, 8)
    sl_pct  = round((price - sl) / price * 100, 2)
    tp1_pct = round((tp1 - price) / price * 100, 2)
    tp2_pct = round((tp2 - price) / price * 100, 2)
    rr = round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0
    risk_level = '低' if signal_score >= 60 and sl_pct < 3 else '高' if signal_score < 40 or sl_pct > 5 else '中'
    return {
        'stop_loss': sl, 'sl_pct': sl_pct,
        'take_profit_1': tp1, 'tp1_pct': tp1_pct,
        'take_profit_2': tp2, 'tp2_pct': tp2_pct,
        'risk_reward': rr, 'risk_level': risk_level,
    }

# ─── 全量分析 ────────────────────────────────────────────────

def full_analysis(coin):
    cfg = COIN_CONFIG[coin]
    symbol = cfg['symbol']
    ticker = get_ticker(coin)
    if not ticker: return None
    price = ticker['price']

    result = {
        'coin': coin, 'name': cfg['name'],
        'price': price,
        'change_24h': ticker['change'],
        'high_24h': ticker['high'],
        'low_24h': ticker['low'],
        'quote_volume_24h': round(ticker['quoteVolume'] / 1e6, 2),
    }

    # K线
    klines = {}
    for iv, lim in [('15m',200),('1h',200),('4h',200),('1d',100)]:
        klines[iv] = get_klines(symbol, iv, lim)

    k15 = klines.get('15m')
    k1h = klines.get('1h')
    k4h = klines.get('4h')
    k1d = klines.get('1d')

    # 1H指标
    if k1h:
        c = k1h['closes']
        result['ma5_1h']   = ma(c, 5)
        result['ma10_1h']  = ma(c, 10)
        result['ma20_1h']  = ma(c, 20)
        result['ma50_1h']  = ma(c, 50)
        result['ema20_1h'] = ema(c, 20)
        result['ema50_1h'] = ema(c, 50)
        result['rsi_1h']   = rsi(c)
        m, s, h = macd(c)
        result['macd_1h'], result['macd_signal_1h'], result['macd_hist_1h'] = m, s, h
        bl, bm, bu = bollinger(c)
        result['bb_lower_1h'], result['bb_mid_1h'], result['bb_upper_1h'] = bl, bm, bu
        result['atr_1h'] = atr(k1h['highs'], k1h['lows'], c)
        result['vol_ratio_1h'] = vol_ratio(k1h['volumes'])

    if k4h:
        c = k4h['closes']
        result['ma20_4h'] = ma(c, 20)
        result['rsi_4h']  = rsi(c)
        m4, _, _ = macd(c)
        result['macd_4h'] = m4

    if k1d:
        c = k1d['closes']
        result['ma50_1d']  = ma(c, 50)
        result['ma200_1d'] = ma(c, 200)
        result['rsi_1d']   = rsi(c)

    # 多周期趋势
    result['timeframe_trends'] = [
        trend_by_tf(k15, '15分钟'),
        trend_by_tf(k1h, '1小时'),
        trend_by_tf(k4h, '4小时'),
        trend_by_tf(k1d, '日线'),
    ]

    # 成交量分析
    va = volume_analysis(k1h)
    result['vol_ratio_1h'] = va.get('vol_ratio')
    result['volume_signal'] = va.get('signal', '')
    result['volume_signal_detail'] = va.get('signal_detail', '')

    # 支撑压力
    sr = support_resistance(k1h, k4h, k1d, price)
    result['resistance'] = sr['resistance']
    result['support']    = sr['support']
    # 兼容旧字段
    result['resistance_levels'] = [r['price'] for r in sr['resistance']]
    result['support_levels']    = [s['price'] for s in sr['support']]

    # 日内/周内高低
    if k1h:
        result['intraday_high'] = round(max(k1h['highs'][-24:]), 8)
        result['intraday_low']  = round(min(k1h['lows'][-24:]), 8)
    if k1d:
        result['week_high'] = round(max(k1d['highs'][-7:]), 8)
        result['week_low']  = round(min(k1d['lows'][-7:]), 8)

    # 短线预测区间
    result['predicted_range'] = predict_range(k1h, price, result.get('atr_1h'))

    # 评分
    result['scores'] = calc_scores(result, klines)

    # 止盈止损
    result['tp_sl'] = calc_tp_sl(price, result.get('atr_1h'), result['scores']['signal'], sr)

    result['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return result

# ─── 报告生成 ────────────────────────────────────────────────

def generate_report(data):
    if not data: return '数据获取失败'
    if ANTHROPIC_API_KEY:
        try:
            scores = data.get('scores', {})
            sr = data.get('resistance', [])
            ss = data.get('support', [])
            pr = data.get('predicted_range', {})
            tp_sl = data.get('tp_sl', {})
            tf_trends = data.get('timeframe_trends', [])
            prompt = f"""你是专业加密货币短线交易分析师。根据数据生成简洁报告，中文输出。

币种：{data['coin']}/USDT（{data.get('name','')}）
价格：${data['price']:,.4f}  24H涨跌：{data.get('change_24h',0):.2f}%
成交量：{data.get('quote_volume_24h',0):.1f}M USDT

评分系统：
- 趋势分：{scores.get('trend','-')}/100
- 动能分：{scores.get('momentum','-')}/100
- 量能分：{scores.get('volume','-')}/100
- 风险分：{scores.get('risk','-')}/100（越高越危险）
- 综合信号：{scores.get('signal','-')}/100 → {scores.get('signal_text','')}

多周期趋势：{' | '.join([f"{t['label']}:{t['trend']}" for t in tf_trends])}
成交量信号：{data.get('volume_signal','')} {data.get('volume_signal_detail','')}

压力位：{[f"${r['price']:,.2f}({r['label']})" for r in sr[:3]]}
支撑位：{[f"${s['price']:,.2f}({s['label']})" for s in ss[:3]]}
预测区间：${pr.get('low',0):,.2f} ~ ${pr.get('high',0):,.2f}（{pr.get('range_pct',0)}%）

止损：${tp_sl.get('stop_loss',0):,.4f}（-{tp_sl.get('sl_pct',0):.2f}%）
止盈1：${tp_sl.get('take_profit_1',0):,.4f}（+{tp_sl.get('tp1_pct',0):.2f}%）
止盈2：${tp_sl.get('take_profit_2',0):,.4f}（+{tp_sl.get('tp2_pct',0):.2f}%）
盈亏比：{tp_sl.get('risk_reward',0):.2f}  风险等级：{tp_sl.get('risk_level','')}

RSI(1H)={data.get('rsi_1h','-')} RSI(4H)={data.get('rsi_4h','-')} MACD={data.get('macd_1h','-')}

请按以下格式输出（每项1-2句，不要啰嗦）：
【综合判断】
【趋势分析】
【关键价位】
【操作建议】
【风险提示】"""
            resp = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                json={'model': 'claude-sonnet-4-20250514', 'max_tokens': 600, 'messages': [{'role': 'user', 'content': prompt}]},
                timeout=30
            )
            return resp.json()['content'][0]['text']
        except: pass

    # 规则报告
    scores = data.get('scores', {})
    tp_sl = data.get('tp_sl', {})
    pr = data.get('predicted_range', {})
    lines = []
    lines.append(f"【综合判断】{scores.get('signal_text','--')}（信号分{scores.get('signal','--')}/100）{scores.get('signal_reason','')}")
    tf = data.get('timeframe_trends', [])
    if tf:
        tf_str = ' | '.join([f"{t['label']}{t['trend']}" for t in tf])
        lines.append(f"【多周期趋势】{tf_str}")
    lines.append(f"【成交量】{data.get('volume_signal','')} {data.get('volume_signal_detail','')}")
    sr = data.get('resistance', [])
    ss = data.get('support', [])
    res_str = '、'.join([f"${r['price']:,.2f}" for r in sr[:2]]) if sr else '暂无'
    sup_str = '、'.join([f"${s['price']:,.2f}" for s in ss[:2]]) if ss else '暂无'
    lines.append(f"【关键价位】压力：{res_str} | 支撑：{sup_str}")
    lines.append(f"【预测区间】短线波动区间 ${pr.get('low',0):,.4f} ~ ${pr.get('high',0):,.4f}")
    lines.append(f"【止盈止损】止损 ${tp_sl.get('stop_loss',0):,.4f}（-{tp_sl.get('sl_pct',0):.2f}%）| 止盈1 ${tp_sl.get('take_profit_1',0):,.4f}（+{tp_sl.get('tp1_pct',0):.2f}%）| 盈亏比 {tp_sl.get('risk_reward',0):.2f}")
    lines.append(f"【风险等级】{tp_sl.get('risk_level','中')} | {scores.get('risk_reason','')}")
    return '\n'.join(lines)

# ─── Telegram ────────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}, timeout=10)
        return r.status_code == 200
    except: return False

# ─── 价格监控线程 ────────────────────────────────────────────

def check_alerts():
    while True:
        try:
            for coin in COIN_CONFIG:
                t = get_ticker(coin)
                if t: price_cache[coin] = t['price']
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
                    send_telegram(f"{sym} <b>价格提醒触发！</b>\n币种：{coin}/USDT\n条件：{'高于' if cond=='above' else '低于'} ${target:,.4f}\n当前：${current:,.4f}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    history.insert(0, {'coin':coin,'condition':cond,'target':target,'price':current,'time':datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                    history = history[:50]
            if changed: save_json(ALERTS_FILE, alerts); save_json(HISTORY_FILE, history)
        except Exception as e: print(f"Monitor error: {e}")
        time.sleep(30)

# ─── 路由 ────────────────────────────────────────────────────

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/prices')
def api_prices(): return jsonify(price_cache)

@app.route('/api/coins')
def api_coins(): return jsonify(list(COIN_CONFIG.keys()))

@app.route('/api/analysis/<coin>')
def api_analysis(coin):
    coin = coin.upper()
    if coin not in COIN_CONFIG: return jsonify({'error': 'invalid coin'})
    data = full_analysis(coin)
    return jsonify(data) if data else jsonify({'error': 'fetch failed'})

@app.route('/api/report/<coin>')
def api_report(coin):
    coin = coin.upper()
    if coin not in COIN_CONFIG: return jsonify({'error': 'invalid coin'})
    data = full_analysis(coin)
    if not data: return jsonify({'error': 'fetch failed'})
    data['report'] = generate_report(data)
    sc = data.get('scores', {})
    tp = data.get('tp_sl', {})
    msg = (f"📊 <b>{coin}/USDT 行情分析</b>\n"
           f"价格：${data['price']:,.4f} ({data['change_24h']:+.2f}%)\n"
           f"信号：{sc.get('signal_text','')}（{sc.get('signal','-')}/100）\n"
           f"趋势:{sc.get('trend','-')} 动能:{sc.get('momentum','-')} 量能:{sc.get('volume','-')} 风险:{sc.get('risk','-')}\n"
           f"止损：${tp.get('stop_loss',0):,.4f} 止盈1：${tp.get('take_profit_1',0):,.4f}\n\n"
           f"{data['report']}")
    send_telegram(msg)
    return jsonify(data)

@app.route('/api/alerts', methods=['GET'])
def get_alerts(): return jsonify(load_json(ALERTS_FILE, []))

@app.route('/api/alerts', methods=['POST'])
def add_alert():
    d = request.json
    alerts = load_json(ALERTS_FILE, [])
    alerts.append({'id':int(time.time()*1000),'coin':d['coin'],'condition':d['condition'],'price':float(d['price']),'triggered':False,'created':datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    save_json(ALERTS_FILE, alerts)
    return jsonify({'ok': True})

@app.route('/api/alerts/<int:aid>', methods=['DELETE'])
def del_alert(aid):
    save_json(ALERTS_FILE, [a for a in load_json(ALERTS_FILE,[]) if a['id']!=aid])
    return jsonify({'ok': True})

@app.route('/api/history')
def get_history(): return jsonify(load_json(HISTORY_FILE, []))

threading.Thread(target=check_alerts, daemon=True).start()