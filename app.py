from flask import Flask, render_template, request, jsonify
import requests
import json
import os
import threading
import time
import numpy as np
from datetime import datetime

app = Flask(__name__)

ALERTS_FILE = 'alerts.json'
HISTORY_FILE = 'history.json'
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

price_cache = {'BTC': 0, 'ETH': 0, 'last_update': ''}

# ─── 工具函数 ───────────────────────────────────────────────

def load_json(filename, default):
    try:
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                return json.load(f)
    except:
        pass
    return default

def save_json(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─── 行情数据获取 ────────────────────────────────────────────

def get_ticker(symbol):
    """获取实时行情 ticker"""
    coin_id = 'bitcoin' if symbol == 'BTCUSDT' else 'ethereum'
    try:
        url = f'https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&community_data=false&developer_data=false'
        r = requests.get(url, timeout=15)
        d = r.json()
        md = d['market_data']
        return {
            'price': md['current_price']['usd'],
            'change': md['price_change_percentage_24h'],
            'high': md['high_24h']['usd'],
            'low': md['low_24h']['usd'],
            'volume': md['total_volume']['usd'] / md['current_price']['usd'],
            'quoteVolume': md['total_volume']['usd'],
        }
    except:
        return None


def get_klines(symbol, interval, limit=200):
    """获取K线数据，多源备用"""
    # 先试 Binance US
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
                if isinstance(data, list) and len(data) > 0:
                    return {
                        'closes':  [float(k[4]) for k in data],
                        'highs':   [float(k[2]) for k in data],
                        'lows':    [float(k[3]) for k in data],
                        'volumes': [float(k[5]) for k in data],
                        'times':   [int(k[0]) for k in data],
                    }
        except:
            continue
    return None


# ─── 技术指标计算 ────────────────────────────────────────────

def calc_ma(closes, n):
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)

def calc_ema(closes, n):
    if len(closes) < n:
        return None
    k = 2 / (n + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 2)

def calc_rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)

def calc_macd(closes):
    if len(closes) < 26:
        return None, None, None
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd = round(ema12 - ema26, 4)
    # signal: EMA9 of MACD (simplified)
    macd_series = []
    for i in range(26, len(closes)+1):
        e12 = calc_ema(closes[:i], 12)
        e26 = calc_ema(closes[:i], 26)
        macd_series.append(e12 - e26)
    signal = calc_ema(macd_series, 9) if len(macd_series) >= 9 else macd
    signal = round(signal, 4) if signal else macd
    hist = round(macd - signal, 4)
    return macd, signal, hist

def calc_bb(closes, n=20, k=2):
    if len(closes) < n:
        return None, None, None
    recent = closes[-n:]
    mid = sum(recent) / n
    std = (sum((x - mid)**2 for x in recent) / n) ** 0.5
    return round(mid - k*std, 2), round(mid, 2), round(mid + k*std, 2)

def calc_atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return round(sum(trs[-n:]) / n, 2)

def calc_volume_ratio(volumes):
    """成交量比（当前/20日均量）"""
    if len(volumes) < 21:
        return None
    avg = sum(volumes[-21:-1]) / 20
    if avg == 0:
        return None
    return round(volumes[-1] / avg, 2)

# ─── 支撑/压力位识别 ─────────────────────────────────────────

def find_swing_points(highs, lows, window=5):
    """找swing high/low"""
    swing_highs, swing_lows = [], []
    for i in range(window, len(highs) - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-window:i+window+1]):
            swing_lows.append(lows[i])
    return swing_highs[-3:], swing_lows[-3:]

def find_support_resistance(klines_1h, klines_1d, current_price):
    """综合识别支撑压力位"""
    result = {}
    if klines_1h:
        h, l = klines_1h['highs'], klines_1h['lows']
        result['intraday_high'] = round(max(h[-24:]), 2)
        result['intraday_low'] = round(min(l[-24:]), 2)
        sh, sl = find_swing_points(h, l)
        result['swing_highs'] = [round(x, 2) for x in sh]
        result['swing_lows'] = [round(x, 2) for x in sl]
    if klines_1d:
        h, l = klines_1d['highs'], klines_1d['lows']
        result['week_high'] = round(max(h[-7:]), 2)
        result['week_low'] = round(min(l[-7:]), 2)
        result['month_high'] = round(max(h[-30:]), 2)
        result['month_low'] = round(min(l[-30:]), 2)

    # 上方压力位（高于当前价）
    all_res = []
    for k in ['intraday_high', 'week_high', 'month_high']:
        if k in result and result[k] > current_price:
            all_res.append(result[k])
    all_res += [x for x in result.get('swing_highs', []) if x > current_price]
    result['resistance_levels'] = sorted(set(all_res))[:3]

    # 下方支撑位（低于当前价）
    all_sup = []
    for k in ['intraday_low', 'week_low', 'month_low']:
        if k in result and result[k] < current_price:
            all_sup.append(result[k])
    all_sup += [x for x in result.get('swing_lows', []) if x < current_price]
    result['support_levels'] = sorted(set(all_sup), reverse=True)[:3]

    return result

# ─── 市场状态判断 ────────────────────────────────────────────

def judge_market_state(indicators):
    """判断市场状态"""
    rsi = indicators.get('rsi_1h')
    macd = indicators.get('macd_1h')
    ma20 = indicators.get('ma20_1h')
    price = indicators.get('price')
    vol_ratio = indicators.get('vol_ratio_1h')

    if None in [rsi, price, ma20]:
        return '数据不足'

    score = 0
    if price > ma20: score += 1
    else: score -= 1
    if rsi and rsi > 60: score += 1
    elif rsi and rsi < 40: score -= 1
    if macd and macd > 0: score += 1
    elif macd and macd < 0: score -= 1
    if vol_ratio and vol_ratio > 1.5: score += 1 if score > 0 else -1

    if score >= 3: return '强势上涨 🚀'
    elif score == 2: return '弱势上涨 📈'
    elif score == 1 or score == 0: return '震荡整理 ↔️'
    elif score == -1 or score == -2: return '弱势下跌 📉'
    else: return '强势下跌 🔻'

# ─── 全量指标计算 ────────────────────────────────────────────

def get_full_analysis(coin):
    symbol = f'{coin}USDT'
    ticker = get_ticker(symbol)
    if not ticker:
        return None

    price = ticker['price']
    result = {
        'coin': coin, 'price': price,
        'change_24h': ticker['change'],
        'high_24h': ticker['high'],
        'low_24h': ticker['low'],
        'volume_24h': ticker['volume'],
        'quote_volume_24h': round(ticker['quoteVolume'] / 1e6, 2),  # 百万USDT
    }

    # 各周期K线
    intervals = {'5m': 100, '15m': 100, '1h': 200, '4h': 200, '1d': 60}
    klines = {}
    for iv, limit in intervals.items():
        klines[iv] = get_klines(symbol, iv, limit)

    # 1H指标
    k1h = klines.get('1h')
    if k1h:
        c = k1h['closes']
        result['ma5_1h']   = calc_ma(c, 5)
        result['ma10_1h']  = calc_ma(c, 10)
        result['ma20_1h']  = calc_ma(c, 20)
        result['ma50_1h']  = calc_ma(c, 50)
        result['ema20_1h'] = calc_ema(c, 20)
        result['ema50_1h'] = calc_ema(c, 50)
        result['rsi_1h']   = calc_rsi(c)
        m, s, h = calc_macd(c)
        result['macd_1h'], result['macd_signal_1h'], result['macd_hist_1h'] = m, s, h
        bb_l, bb_m, bb_u = calc_bb(c)
        result['bb_lower_1h'], result['bb_mid_1h'], result['bb_upper_1h'] = bb_l, bb_m, bb_u
        result['atr_1h']       = calc_atr(k1h['highs'], k1h['lows'], c)
        result['vol_ratio_1h'] = calc_volume_ratio(k1h['volumes'])

    # 4H指标
    k4h = klines.get('4h')
    if k4h:
        c = k4h['closes']
        result['ma20_4h']  = calc_ma(c, 20)
        result['rsi_4h']   = calc_rsi(c)
        m, s, h = calc_macd(c)
        result['macd_4h']  = m

    # 日线指标
    k1d = klines.get('1d')
    if k1d:
        c = k1d['closes']
        result['ma50_1d']  = calc_ma(c, 50)
        result['ma200_1d'] = calc_ma(c, 200)
        result['rsi_1d']   = calc_rsi(c)

    # 支撑/压力
    sr = find_support_resistance(k1h, k1d, price)
    result.update(sr)

    # 市场状态
    result['market_state'] = judge_market_state(result)

    result['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return result

# ─── AI 分析报告 ─────────────────────────────────────────────

def generate_ai_report(analysis):
    """调用 Claude API 生成分析报告"""
    if not ANTHROPIC_API_KEY:
        return generate_simple_report(analysis)
    try:
        coin = analysis['coin']
        price = analysis['price']
        prompt = f"""你是一位专业的加密货币短线交易分析师。根据以下数据，生成一份简洁专业的短线交易分析报告。

币种：{coin}/USDT
当前价格：${price:,.2f}
24H涨跌：{analysis.get('change_24h', 0):.2f}%
24H高点：${analysis.get('high_24h', 0):,.2f}
24H低点：${analysis.get('low_24h', 0):,.2f}
成交量（百万USDT）：{analysis.get('quote_volume_24h', 0):.1f}M

技术指标（1H）：
- RSI: {analysis.get('rsi_1h', 'N/A')}
- MACD: {analysis.get('macd_1h', 'N/A')} / Signal: {analysis.get('macd_signal_1h', 'N/A')}
- MA20: {analysis.get('ma20_1h', 'N/A')} | MA50: {analysis.get('ma50_1h', 'N/A')}
- 布林带: {analysis.get('bb_lower_1h', 'N/A')} ~ {analysis.get('bb_upper_1h', 'N/A')}
- ATR: {analysis.get('atr_1h', 'N/A')}
- 成交量比: {analysis.get('vol_ratio_1h', 'N/A')}x

4H RSI: {analysis.get('rsi_4h', 'N/A')} | 日线RSI: {analysis.get('rsi_1d', 'N/A')}
市场状态：{analysis.get('market_state', 'N/A')}

压力位：{analysis.get('resistance_levels', [])}
支撑位：{analysis.get('support_levels', [])}

请用中文输出以下格式（每项一行，简洁）：
【趋势判断】
【上方压力】
【下方支撑】
【短线目标】高点 / 低点
【突破信号】
【止盈建议】
【止损建议】
【风险等级】低/中/高
【综合建议】2-3句话"""

        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json'
            },
            json={
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 800,
                'messages': [{'role': 'user', 'content': prompt}]
            },
            timeout=30
        )
        data = resp.json()
        return data['content'][0]['text']
    except Exception as e:
        return generate_simple_report(analysis)

def generate_simple_report(a):
    """无AI时的简单规则报告"""
    lines = []
    price = a.get('price', 0)
    rsi = a.get('rsi_1h')
    macd = a.get('macd_1h')
    bb_u = a.get('bb_upper_1h')
    bb_l = a.get('bb_lower_1h')
    atr = a.get('atr_1h', 0) or 0

    lines.append(f"【趋势判断】{a.get('market_state', '数据不足')}")

    res = a.get('resistance_levels', [])
    sup = a.get('support_levels', [])
    lines.append(f"【上方压力】{'、'.join([f'${x:,.0f}' for x in res]) if res else '暂无明显压力'}")
    lines.append(f"【下方支撑】{'、'.join([f'${x:,.0f}' for x in sup]) if sup else '暂无明显支撑'}")

    target_high = res[0] if res else round(price + atr * 2, 2)
    target_low  = sup[0] if sup else round(price - atr * 2, 2)
    lines.append(f"【短线目标】高点 ${target_high:,.2f} / 低点 ${target_low:,.2f}")

    if macd and a.get('macd_hist_1h'):
        hist = a['macd_hist_1h']
        lines.append(f"【突破信号】MACD柱{'扩大中，动能增强' if hist > 0 else '收缩中，注意反转'}")
    else:
        lines.append("【突破信号】数据计算中")

    lines.append(f"【止盈建议】${target_high:,.2f} 附近分批止盈")
    stop = round(price - atr * 1.5, 2)
    lines.append(f"【止损建议】跌破 ${stop:,.2f} 止损（约 {abs(price-stop)/price*100:.1f}%）")

    if rsi:
        if rsi > 70: risk = '高（RSI超买）'
        elif rsi < 30: risk = '高（RSI超卖）'
        elif 50 < rsi <= 70: risk = '中'
        else: risk = '低'
    else:
        risk = '中'
    lines.append(f"【风险等级】{risk}")
    lines.append(f"【综合建议】当前{a.get('market_state','震荡')}，建议结合成交量确认方向后入场，严格执行止损。")

    return '\n'.join(lines)

# ─── 价格监控线程 ────────────────────────────────────────────

def check_alerts():
    while True:
        try:
            for coin in ['BTC', 'ETH']:
                ticker = get_ticker(f'{coin}USDT')
                if ticker:
                    price_cache[coin] = ticker['price']
            price_cache['last_update'] = datetime.now().strftime('%H:%M:%S')

            alerts = load_json(ALERTS_FILE, [])
            history = load_json(HISTORY_FILE, [])
            changed = False
            for alert in alerts:
                if alert.get('triggered'):
                    continue
                coin = alert['coin']
                current = price_cache.get(coin, 0)
                target = float(alert['price'])
                cond = alert['condition']
                triggered = (cond == 'above' and current > target) or \
                            (cond == 'below' and current < target)
                if triggered:
                    alert['triggered'] = True
                    changed = True
                    symbol = '📈' if cond == 'above' else '📉'
                    msg = (f"{symbol} <b>价格提醒触发！</b>\n"
                           f"币种：{coin}/USDT\n"
                           f"条件：{'高于' if cond == 'above' else '低于'} ${target:,.2f}\n"
                           f"当前价格：${current:,.2f}\n"
                           f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    send_telegram(msg)
                    history.insert(0, {'coin': coin, 'condition': cond,
                        'target': target, 'price': current,
                        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                    history = history[:50]
            if changed:
                save_json(ALERTS_FILE, alerts)
                save_json(HISTORY_FILE, history)
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(30)

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        r = requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID,
            'text': message, 'parse_mode': 'HTML'}, timeout=10)
        return r.status_code == 200
    except:
        return False

# ─── API 路由 ────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/prices')
def api_prices():
    return jsonify(price_cache)

@app.route('/api/analysis/<coin>')
def api_analysis(coin):
    coin = coin.upper()
    if coin not in ['BTC', 'ETH']:
        return jsonify({'error': 'invalid coin'})
    data = get_full_analysis(coin)
    if not data:
        return jsonify({'error': 'fetch failed'})
    return jsonify(data)

@app.route('/api/report/<coin>')
def api_report(coin):
    coin = coin.upper()
    data = get_full_analysis(coin)
    if not data:
        return jsonify({'error': 'fetch failed'})
    report = generate_ai_report(data)
    data['report'] = report
    # 发送到 Telegram
    msg = f"📊 <b>{coin}/USDT 行情分析</b>\n价格：${data['price']:,.2f} ({data['change_24h']:+.2f}%)\n\n{report}"
    send_telegram(msg)
    return jsonify(data)

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    return jsonify(load_json(ALERTS_FILE, []))

@app.route('/api/alerts', methods=['POST'])
def add_alert():
    data = request.json
    alerts = load_json(ALERTS_FILE, [])
    alerts.append({'id': int(time.time()*1000), 'coin': data['coin'],
        'condition': data['condition'], 'price': float(data['price']),
        'triggered': False, 'created': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    save_json(ALERTS_FILE, alerts)
    return jsonify({'ok': True})

@app.route('/api/alerts/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    alerts = [a for a in load_json(ALERTS_FILE, []) if a['id'] != alert_id]
    save_json(ALERTS_FILE, alerts)
    return jsonify({'ok': True})

@app.route('/api/history')
def get_history():
    return jsonify(load_json(HISTORY_FILE, []))

t = threading.Thread(target=check_alerts, daemon=True)
t.start()
