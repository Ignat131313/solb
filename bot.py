import os
import json
import requests
import asyncio
import websockets
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey as PublicKey
from solana.transaction import Transaction
from base58 import b58decode, b58encode
from flask import Flask, render_template, request, jsonify

# Добавим проверку установки solana
try:
    import solana
    print(f"Solana version: {solana.__version__}")
except ImportError as e:
    print(f"Ошибка импорта solana: {str(e)}")

# Константы
GMGN_API_HOST = "https://gmgn.ai"
PUMP_API_HOST = "https://pumpportal.fun/api"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
CONFIG_FILE = "config.json"

# Flask приложение
app = Flask(__name__)

# Загрузка приватного ключа
PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
if not PRIVATE_KEY:
    raise ValueError("Установите SOLANA_PRIVATE_KEY в переменных окружения")

try:
    client = Client(SOLANA_RPC)
    keypair = Keypair.from_bytes(b58decode(PRIVATE_KEY))
    WALLET_ADDRESS = str(PublicKey(keypair.pubkey()))
except Exception as e:
    raise ValueError(f"Ошибка при создании ключа: {str(e)}")

# Загрузка конфигурации
def load_config():
    default_config = {
        "blacklisted_tokens": [],
        "blacklisted_devs": [],
        "filters": {"min_market_cap": 10000, "max_market_cap": 1000000, "min_holders": 50, "max_holders": 10000}
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            default_config.update(config)
    else:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f, indent=2)
    return default_config

CONFIG = load_config()

# Статистика
stats = {
    "spent": 0.0,  # Потрачено в SOL
    "profit": 0.0,  # Прибыль в SOL
    "trades": [],  # Список свопов для подсчёта вин рейта
}

# Функции GMGN API
def get_token_info(token_address: str) -> dict:
    url = f"{GMGN_API_HOST}/defi/router/v1/sol/token_info?token_address={token_address}"
    response = requests.get(url)
    return response.json() if response.status_code == 200 else {}

def get_swap_route(input_token: str, output_token: str, amount: str, from_address: str, slippage: float = 0.5) -> dict:
    url = f"{GMGN_API_HOST}/defi/router/v1/sol/tx/get_swap_route?token_in_address={input_token}&token_out_address={output_token}&in_amount={amount}&from_address={from_address}&slippage={slippage}"
    response = requests.get(url)
    return response.json() if response.status_code == 200 else {}

def submit_signed_transaction(signed_tx: str) -> dict:
    url = f"{GMGN_API_HOST}/defi/router/v1/sol/tx/submit_signed_transaction"
    response = requests.post(url, json={"signed_tx": signed_tx}, headers={"content-type": "application/json"})
    return response.json() if response.status_code == 200 else {}

# Фильтрация токенов
def is_token_filtered(token_address: str, token_info: dict) -> bool:
    if token_address in CONFIG["blacklisted_tokens"]:
        return True
    dev_address = token_info.get("dev_address", "")
    if dev_address in CONFIG["blacklisted_devs"]:
        return True
    market_cap = float(token_info.get("market_cap", 0))
    holders = int(token_info.get("holders", 0))
    filters = CONFIG["filters"]
    return not (filters["min_market_cap"] <= market_cap <= filters["max_market_cap"] and filters["min_holders"] <= holders <= filters["max_holders"])

# Логика свопа
def perform_swap(input_token: str, output_token: str, amount: str, slippage: float = 0.5) -> str:
    route = get_swap_route(input_token, output_token, amount, WALLET_ADDRESS, slippage)
    if not route or "data" not in route:
        return "Не удалось получить маршрут свопа"
    swap_tx_base64 = route["data"]["raw_tx"]["swapTransaction"]
    swap_tx_buf = b58decode(swap_tx_base64)
    transaction = Transaction.deserialize(swap_tx_buf)
    transaction.sign(keypair)
    signed_tx = b58encode(transaction.serialize()).decode("utf-8")
    result = submit_signed_transaction(signed_tx)
    
    # Обновление статистики
    amount_float = float(amount) / 10**9  # Перевод из лампортов в SOL
    stats["spent"] += amount_float
    profit = result.get("profit", 0.0) if "profit" in result else 0.0
    stats["profit"] += profit
    stats["trades"].append({"profit": profit})
    
    return f"Результат свопа: {json.dumps(result, indent=2)}"

# Продать все (заглушка)
def sell_all():
    return "Все позиции проданы (заглушка)"

# Список токенов для интерфейса
tokens_list = []

async def scan_trending_tokens():
    uri = "wss://pumpportal.fun/api/data"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({"method": "subscribeNewToken"}))
        while True:
            message = await websocket.recv()
            token_data = json.loads(message)
            token_address = token_data.get("token_address")
            if token_address:
                token_info = get_token_info(token_address)
                if token_info and not is_token_filtered(token_address, token_info):
                    token_stats = {
                        "address": token_address,
                        "market_cap": token_info.get("market_cap", "N/A"),
                        "holders": token_info.get("holders", "N/A"),
                        "liquidity": token_info.get("liquidity", "N/A"),
                        "volume": token_info.get("volume", "N/A")
                    }
                    tokens_list.append(token_stats)
                    if len(tokens_list) > 50:
                        tokens_list.pop(0)

# Маршруты Flask
@app.route('/')
def index():
    win_rate = sum(1 for trade in stats["trades"] if trade["profit"] > 0) / len(stats["trades"]) * 100 if stats["trades"] else 0
    return render_template('index.html', tokens=tokens_list, stats=stats, win_rate=win_rate)

@app.route('/swap', methods=['POST'])
def swap():
    data = request.json
    result = perform_swap(data["input_token"], data["output_token"], data["amount"])
    return jsonify({"result": result})

@app.route('/sell_all', methods=['POST'])
def sell_all_route():
    result = sell_all()
    return jsonify({"result": result})

# Запуск WebSocket в фоновом режиме
def start_websocket():
    asyncio.run(scan_trending_tokens())

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_websocket, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5000)
