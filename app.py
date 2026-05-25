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
    cfg = COIN_CONFIG[coin]
    cg_id = cfg['cg_id']
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price'
        params = {'ids': cg_id, 'vs_currencies': 'usd',
                  'include_24hr_change': 'true', 'include_24hr_vol': 'true'}
        r = requests.get(url, params=params, timeout=15, headers=HEADERS)
        if r.status_code == 200:
            d = r.json()[cg_id]
            price = d['usd']
            return {
                'price': price,
                'change': d.get('usd_24h_change', 0) or 0,
                'high': price * 1.01,
                'low': price * 0.99,
                'volume': d.get('usd_24h_vol', 0) / max(price, 0.0001),
                'quoteVolume': d.get('usd_24h_vol', 0),
            }
    except: pass
    try:
        time.sleep(1)
        url = f'https://api.coingecko.com/api/v3/coins/{cg_id}?localization=false&tickers=false&community_data=false&developer_data=false'
        r = requests.get(url, timeout=20, headers=HEADERS)
        if r.status_code == 200:
            d = r.json()['market_data']
            return {
                'price': d['current_price']['usd'],
                'change': d['price_change_percentage_24h'] or 0,
                'high': d['high_24h']['usd'],
                'low': d['low_24h']['usd'],
                'volume': d['total_volume']['usd'] / max(d['current_price']['usd'], 0.0001),
                'quoteVolume': d['total_volume']['usd'],
            }
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
    result = {'vol_ratio': vr}
    if not vr: return result
    price_change = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
    if vr >= 2.0 and price_change > 0.5:
        result['signal'] = '🚀 放量突破'
        result['signal_detail'] = '成交量是均量' + str(round(vr,1)) + '倍，价格上涨' + str(round(price_change,1)) + '%'
    elif vr >= 1.5 and price_change > 0:
        result['signal'] = '📈 温和放量上涨'
        result['signal_detail'] = '量比' + str(vr) + 'x，温和上涨'
    elif vr >= 2.0 and price_change < -0.5:
        result['signal'] = '🔻 放量下跌'
        result['signal_detail'] = '成交量' + str(round(vr,1)) + '倍，下行压力大'
    elif vr < 0.5:
        result['signal'] = '😴 缩量横盘'
        result['signal_detail'] = '量比仅' + str(vr) + 'x，方向待定'
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
    tf_scores = [t['score'] for t in data.get('timeframe_trends', []) if 'score' in t]
    trend_score = round(sum(tf_scores) / len(tf_scores)) if tf_scores else 50
    scores['trend'] = trend_score
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
    vr = data.get('vol_ratio_1h')
    vol_sig = data.get('volume_signal', '')
    vol = 50
    if vr:
        if vr >= 2.0: vol = 85 if '上涨' in vol_sig or '突破' in vol_sig else 30
        elif vr >= 1.5: vol = 70 if price > (data.get('ma20_1h') or price) else 45
        elif vr < 0.5: vol = 35
        else: vol = 55
    scores['volume'] = max(0, min(100, round(vol)))
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
    signal = round(trend_score*0.35 + scores['momentum']*0.30 + scores['volume']*0.20 + (100-scores['risk'])*0.15)
    scores['signal'] = max(0, min(100, signal))
    if scores['signal'] >= 80: scores['signal_text'] = '强烈看多 🚀'; scores['signal_class'] = 'bull'
    elif scores['signal'] >= 60: scores['signal_text'] = '偏多 📈'; scores['signal_class'] = 'bull'
    elif scores['signal'] >= 40: scores['signal_text'] = '震荡观望 ↔️'; scores['signal_class'] = 'neutral'
    elif scores['signal'] >= 20: scores['signal_text'] = '偏空 📉'; scores['signal_class'] = 'bear'
    else: scores['signal_text'] = '强烈看空 🔻'; scores['signal_class'] = 'bear'
    scores['trend_reason'] = '多周期均线趋势' + ('偏强' if trend_score > 60 else '偏弱' if trend_score < 40 else '中性')
    scores['momentum_reason'] = 'RSI=' + str(rsi_1h or '--') + ' MACD' + ('正值' if macd_h and macd_h > 0 else '负值' if macd_h else '计算中')
    scores['volume_reason'] = '量比' + str(vr or '--') + 'x ' + str(data.get('volume_signal_detail', ''))
    scores['risk_reason'] = 'RSI' + ('超买' if rsi_v > 70 else '超卖' if rsi_v < 30 else '正常') + ' 24H波动' + str(round(abs(data.get('change_24h', 0)), 1)) + '%'
    scores['signal_reason'] = '趋势35%+动能30%+量能20%+风险15% → ' + scores['signal_text']
    return scores

def calc_tp_sl(price, atr_val, signal_score, sr):
    if not atr_val: atr_val = price * 0.02
    res_list = sr.get('resistance', [])
    sup_list = sr.get('support', [])
    sl = round(sup_list[0]['price'] * 0.998, 4) if sup_list else round(price - atr_val * 1.5, 4)
    tp1 = round(res_list[0]['price'] * 0.998, 4) if res_list else round(price + atr_val * 2, 4)
    tp2 = round(res_list[1]['price'] * 0.998, 4) if len(res_list) > 1 else round(price + atr_val * 4, 4)
    sl_pct  = round((price - sl) / price * 100, 2)
    tp1_pct = round((tp1 - price) / price * 100, 2)
    tp2_pct = round((tp2 - price) / price * 100, 2)
    rr = round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0
    risk_level = '低' if signal_score >= 60 and sl_pct < 3 else '高' if signal_score < 40 or sl_pct > 5 else '中'
    return {'stop_loss': sl, 'sl_pct': sl_pct, 'take_profit_1': tp1, 'tp1_pct': tp1_pct,
            'take_profit_2': tp2, 'tp2_pct': tp2_pct, 'risk_reward': rr, 'risk_level': risk_level}

def full_analysis(coin):
    cfg = COIN_CONFIG[coin]
    symbol = cfg['symbol']
    ticker = get_ticker(coin)
    if not ticker: return None
    price = ticker['price']
    result = {'coin': coin, 'name': cfg['name'], 'price': price,
              'change_24h': ticker['change'], 'high_24h': ticker['high'],
              'low_24h': ticker['low'], 'quote_volume_24h': round(ticker['quoteVolume']/1e6, 2)}
    klines = {}
    for iv, lim in [('15m',100),('1h',200),('4h',150),('1d',60)]:
        klines[iv] = get_klines(symbol, iv, lim)
        time.sleep(0.2)
    k15=klines.get('15m'); k1h=klines.get('1h'); k4h=klines.get('4h'); k1d=klines.get('1d')
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
    result['updated_at']=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return result
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
            tf_str = ' | '.join([t['label']+':'+t['trend'] for t in tf_trends])
            prompt = '你是专业加密货币短线交易分析师。用中文生成简洁报告。\n'
            prompt += '币种：' + data['coin'] + '/USDT 价格：$' + str(round(data['price'],4)) + '\n'
            prompt += '24H涨跌：' + str(round(data.get('change_24h',0),2)) + '%\n'
            prompt += '信号：' + str(scores.get('signal','-')) + '/100 ' + str(scores.get('signal_text','')) + '\n'
            prompt += '趋势:' + str(scores.get('trend','-')) + ' 动能:' + str(scores.get('momentum','-')) + ' 量能:' + str(scores.get('volume','-')) + ' 风险:' + str(scores.get('risk','-')) + '\n'
            prompt += '多周期：' + tf_str + '\n'
            prompt += '成交量：' + str(data.get('volume_signal','')) + '\n'
            prompt += '压力位：' + str([round(r['price'],2) for r in sr[:3]]) + '\n'
            prompt += '支撑位：' + str([round(s['price'],2) for s in ss[:3]]) + '\n'
            prompt += '预测区间：$' + str(pr.get('low',0)) + '~$' + str(pr.get('high',0)) + '\n'
            prompt += '止损：$' + str(tp_sl.get('stop_loss',0)) + ' 止盈1：$' + str(tp_sl.get('take_profit_1',0)) + '\n'
            prompt += 'RSI(1H)=' + str(data.get('rsi_1h','-')) + '\n'
            prompt += '请输出（每项1-2句）：【综合判断】【趋势分析】【关键价位】【操作建议】【风险提示】'
            resp = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                json={'model': 'claude-sonnet-4-20250514', 'max_tokens': 600,
                      'messages': [{'role': 'user', 'content': prompt}]},
                timeout=30)
            return resp.json()['content'][0]['text']
        except: pass
    scores = data.get('scores', {})
    tp_sl = data.get('tp_sl', {})
    pr = data.get('predicted_range', {})
    sr = data.get('resistance', [])
    ss = data.get('support', [])
    tf = data.get('timeframe_trends', [])
    lines = []
    lines.append('【综合判断】' + str(scores.get('signal_text','--')) + '（信号分' + str(scores.get('signal','--')) + '/100）')
    if tf:
        lines.append('【多周期趋势】' + ' | '.join([t['label']+t['trend'] for t in tf]))
    lines.append('【成交量】' + str(data.get('volume_signal','')) + ' ' + str(data.get('volume_signal_detail','')))
    res_str = '、'.join(['$'+str(round(r['price'],2)) for r in sr[:2]]) if sr else '暂无'
    sup_str = '、'.join(['$'+str(round(s['price'],2)) for s in ss[:2]]) if ss else '暂无'
    lines.append('【关键价位】压力：' + res_str + ' | 支撑：' + sup_str)
    lines.append('【预测区间】$' + str(pr.get('low',0)) + ' ~ $' + str(pr.get('high',0)))
    lines.append('【止盈止损】止损$' + str(tp_sl.get('stop_loss',0)) + '(-' + str(tp_sl.get('sl_pct',0)) + '%) 止盈1$' + str(tp_sl.get('take_profit_1',0)) + '(+' + str(tp_sl.get('tp1_pct',0)) + '%) 盈亏比' + str(tp_sl.get('risk_reward',0)))
    lines.append('【风险等级】' + str(tp_sl.get('risk_level','中')) + ' | ' + str(scores.get('risk_reason','')))
    return '\n'.join(lines)

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
    if coin not in COIN_CONFIG: return jsonify({'error': 'invalid coin'})
    data = full_analysis(coin)
    if not data: return jsonify({'error': 'fetch failed'})
    data['report'] = generate_report(data)
    sc = data.get('scores', {})
    tp = data.get('tp_sl', {})
    msg = ('📊 <b>' + coin + '/USDT 行情分析</b>\n价格：$' + str(round(data['price'],4)) +
           ' (' + ('+' if data['change_24h']>=0 else '') + str(round(data['change_24h'],2)) + '%)\n' +
           '信号：' + str(sc.get('signal_text','')) + '（' + str(sc.get('signal','-')) + '/100）\n' +
           '趋势:' + str(sc.get('trend','-')) + ' 动能:' + str(sc.get('momentum','-')) +
           ' 量能:' + str(sc.get('volume','-')) + ' 风险:' + str(sc.get('risk','-')) + '\n' +
           '止损：$' + str(tp.get('stop_loss',0)) + ' 止盈1：$' + str(tp.get('take_profit_1',0)) + '\n\n' +
           str(data['report']))
    send_telegram(msg)
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
