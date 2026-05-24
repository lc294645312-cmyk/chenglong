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

price_cache = {'BTC': 0, 'ETH': 0, 'last_update': ''}

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

def get_prices():
    try:
        url = 'https://api.binance.com/api/v3/ticker/price'
        r = requests.get(url, timeout=10)
        data = r.json()
        prices = {}
        for item in data:
            if item['symbol'] == 'BTCUSDT':
                prices['BTC'] = float(item['price'])
            elif item['symbol'] == 'ETHUSDT':
                prices['ETH'] = float(item['price'])
        return prices
    except:
        try:
            url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd'
            r = requests.get(url, timeout=10)
            data = r.json()
            return {'BTC': data['bitcoin']['usd'], 'ETH': data['ethereum']['usd']}
        except:
            return None

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        r = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }, timeout=10)
        return r.status_code == 200
    except:
        return False

def check_alerts():
    while True:
        try:
            prices = get_prices()
            if prices:
                price_cache['BTC'] = prices.get('BTC', 0)
                price_cache['ETH'] = prices.get('ETH', 0)
                price_cache['last_update'] = datetime.now().strftime('%H:%M:%S')
                alerts = load_json(ALERTS_FILE, [])
                history = load_json(HISTORY_FILE, [])
                changed = False
                for alert in alerts:
                    if alert.get('triggered'):
                        continue
                    coin = alert['coin']
                    condition = alert['condition']
                    target = float(alert['price'])
                    current = prices.get(coin, 0)
                    triggered = False
                    if condition == 'above' and current > target:
                        triggered = True
                    elif condition == 'below' and current < target:
                        triggered = True
                    if triggered:
                        alert['triggered'] = True
                        changed = True
                        symbol = '📈' if condition == 'above' else '📉'
                        msg = (f"{symbol} <b>价格提醒触发！</b>\n"
                               f"币种：{coin}/USDT\n"
                               f"条件：{'高于' if condition == 'above' else '低于'} ${target:,.2f}\n"
                               f"当前价格：${current:,.2f}\n"
                               f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        send_telegram(msg)
                        history.insert(0, {
                            'coin': coin, 'condition': condition,
                            'target': target, 'price': current,
                            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        })
                        history = history[:50]
                if changed:
                    save_json(ALERTS_FILE, alerts)
                    save_json(HISTORY_FILE, history)
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(30)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/prices')
def api_prices():
    return jsonify(price_cache)

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    return jsonify(load_json(ALERTS_FILE, []))

@app.route('/api/alerts', methods=['POST'])
def add_alert():
    data = request.json
    alerts = load_json(ALERTS_FILE, [])
    alerts.append({
        'id': int(time.time() * 1000),
        'coin': data['coin'],
        'condition': data['condition'],
        'price': float(data['price']),
        'triggered': False,
        'created': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })
    save_json(ALERTS_FILE, alerts)
    return jsonify({'ok': True})

@app.route('/api/alerts/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    alerts = load_json(ALERTS_FILE, [])
    alerts = [a for a in alerts if a['id'] != alert_id]
    save_json(ALERTS_FILE, alerts)
    return jsonify({'ok': True})

@app.route('/api/history')
def get_history():
    return jsonify(load_json(HISTORY_FILE, []))

t = threading.Thread(target=check_alerts, daemon=True)
t.start()
