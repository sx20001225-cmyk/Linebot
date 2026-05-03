"""
LINE Bot AI 助手 - 加密貨幣交易控制系統 (Claude 版)
功能：
1. 接收 LINE 訊息 (Webhook)
2. 用 Claude 解析使用者意圖
3. 查詢 MySQL（持倉、交易記錄、設定）
4. 查詢 Binance API（即時餘額、價格）
5. 修改 bot_config（暫停、恢復、調整參數）
6. 智能對話回應
"""

import os
import json
import hmac
import hashlib
import base64
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import pymysql
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from anthropic import Anthropic

# ============================================================================
# 設定
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# LINE
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_API_URL = "https://api.line.me/v2/bot"

# Anthropic Claude
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', 'claude-haiku-4-5')  # 便宜且快

# MySQL
DATABASE_URL = os.getenv('DATABASE_URL')
USER_ID = int(os.getenv('USER_ID', '1'))

# Binance（用於查餘額）
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

# 安全：只允許這些 LINE User ID 操作機器人（從 LINE Webhook 取得）
ALLOWED_USER_IDS = os.getenv('LINE_ALLOWED_USER_IDS', '').split(',')

# 初始化
app = FastAPI()
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# ============================================================================
# MySQL 連線
# ============================================================================

def parse_database_url(url):
    url = url.replace("mysql://", "")
    auth, rest = url.split("@")
    user, password = auth.split(":")
    host_port, database = rest.split("/")
    if ":" in host_port:
        host, port = host_port.split(":")
        port = int(port)
    else:
        host = host_port
        port = 3306
    return {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database, 'charset': 'utf8mb4',
        'cursorclass': pymysql.cursors.DictCursor,
    }

DB_CONFIG = parse_database_url(DATABASE_URL) if DATABASE_URL else None

def get_db():
    return pymysql.connect(**DB_CONFIG)


# ============================================================================
# LINE 訊息推送
# ============================================================================

def line_reply(reply_token: str, messages: list):
    """回覆 LINE 訊息（reply token 5 分鐘內有效）"""
    headers = {
        'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
        'Content-Type': 'application/json',
    }
    payload = {
        'replyToken': reply_token,
        'messages': messages,
    }
    try:
        resp = requests.post(f"{LINE_API_URL}/message/reply", headers=headers, json=payload, timeout=5)
        if resp.status_code != 200:
            logger.error(f"LINE reply failed: {resp.status_code} {resp.text}")
        return resp
    except Exception as e:
        logger.error(f"LINE reply error: {e}")

def line_push(user_id: str, messages: list):
    """主動推送訊息給使用者（不需要 reply token）"""
    headers = {
        'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
        'Content-Type': 'application/json',
    }
    payload = {
        'to': user_id,
        'messages': messages,
    }
    try:
        resp = requests.post(f"{LINE_API_URL}/message/push", headers=headers, json=payload, timeout=5)
        return resp
    except Exception as e:
        logger.error(f"LINE push error: {e}")


def text_message(text: str) -> dict:
    """建立文字訊息"""
    return {'type': 'text', 'text': text[:5000]}  # LINE 限制 5000 字


def confirm_message(text: str, action_yes: str, action_no: str) -> dict:
    """建立確認按鈕訊息"""
    return {
        'type': 'template',
        'altText': text,
        'template': {
            'type': 'confirm',
            'text': text[:240],
            'actions': [
                {'type': 'postback', 'label': '✅ 確認', 'data': action_yes},
                {'type': 'postback', 'label': '❌ 取消', 'data': action_no},
            ]
        }
    }


# ============================================================================
# 簽章驗證（防止偽造請求）
# ============================================================================

def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return False
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode('utf-8'),
        body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_value).decode('utf-8')
    return hmac.compare_digest(expected, signature)


# ============================================================================
# 資料查詢函數
# ============================================================================

def get_bot_config() -> dict:
    """取得機器人設定"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM bot_config WHERE userId = %s LIMIT 1", (USER_ID,))
            return cursor.fetchone() or {}
    finally:
        conn.close()


def get_recent_trades(limit: int = 5) -> list:
    """取得最近交易"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM trades WHERE userId = %s ORDER BY createdAt DESC LIMIT %s",
                (USER_ID, limit)
            )
            return cursor.fetchall() or []
    finally:
        conn.close()


def get_today_pnl() -> float:
    """取得今日盈虧"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT SUM(pnl) as total FROM trades 
                   WHERE userId = %s AND DATE(createdAt) = CURDATE()""",
                (USER_ID,)
            )
            row = cursor.fetchone()
            return float(row['total'] or 0) if row else 0
    finally:
        conn.close()


def get_binance_balance() -> dict:
    """查詢幣安帳戶餘額"""
    try:
        import time
        params = f"timestamp={int(time.time() * 1000)}"
        signature = hmac.new(
            BINANCE_API_SECRET.encode(),
            params.encode(),
            hashlib.sha256
        ).hexdigest()
        
        # 合約餘額
        resp = requests.get(
            f"https://fapi.binance.com/fapi/v2/balance?{params}&signature={signature}",
            headers={'X-MBX-APIKEY': BINANCE_API_KEY},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            usdt = next((b for b in data if b.get('asset') == 'USDT'), {})
            return {
                'futures_balance': float(usdt.get('balance', 0)),
                'futures_available': float(usdt.get('availableBalance', 0)),
            }
    except Exception as e:
        logger.error(f"Binance balance error: {e}")
    return {'futures_balance': 0, 'futures_available': 0}


def update_bot_config(field: str, value) -> bool:
    """更新機器人設定（限定欄位）"""
    allowed_fields = [
        'isEnabled', 'leverage', 'positionSizeMax', 'dailyLossLimit',
        'stopLoss', 'profitTarget1', 'profitTarget2',
        'monitorPairs', 'longPairs', 'shortPairs',
        'decisionBuyThreshold', 'decisionSellThreshold', 'checkIntervalSeconds',
    ]
    if field not in allowed_fields:
        return False
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # JSON 欄位需要特殊處理
            if field in ['monitorPairs', 'longPairs', 'shortPairs']:
                value = json.dumps(value)
            cursor.execute(
                f"UPDATE bot_config SET {field} = %s WHERE userId = %s",
                (value, USER_ID)
            )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Update config error: {e}")
        return False
    finally:
        conn.close()


# ============================================================================
# GPT 意圖識別與處理
# ============================================================================

SYSTEM_PROMPT = """你是「CryptoBot AI」，一個資深加密貨幣交易顧問與自動交易系統的智能助手。

【你的角色定位】
- 自動交易系統的控制中樞（可查詢/操作系統）
- 加密貨幣市場的專業分析師（會技術分析、風控、心態建議）
- 知識淵博的教學助理（解釋指標、概念、策略）
- 親切的對話夥伴（聊天、心理支持）

【回應規則】
你必須回傳結構化 JSON，**只回傳 JSON，不要有任何其他文字**（不要 markdown code block，不要解釋，直接 JSON）。

【可用的意圖 (intent)】

# 系統查詢類
- `query_status`: 查詢系統狀態（持倉、餘額、運行狀態）
- `query_trades`: 查詢交易記錄
- `query_config`: 查詢設定（槓桿、監控對、止損等）
- `query_pnl`: 查詢盈虧
- `query_market`: 查詢某個幣的即時行情（params: {"symbol": "BTCUSDT"}）

# 系統操作類
- `action_pause`: 暫停機器人
- `action_resume`: 恢復機器人
- `action_change_leverage`: 改變槓桿（params: {"leverage": 5}）
- `action_add_pair`: 新增監控幣種（params: {"pair": "ETHUSDT", "type": "long"}, type: main/long/short）
- `action_remove_pair`: 移除監控幣種（params: {"pair": "ETHUSDT"}）

# 分析與教學類（直接給深度回答）
- `chat`: 用於市場分析、技術指標教學、概念解釋、聊天、心理支持

【JSON 格式】
{"intent": "意圖名稱", "params": {}, "response": "回覆內容"}

【範例】

# 系統查詢
「狀態」→ {"intent": "query_status", "params": {}, "response": ""}
「我今天賺多少」→ {"intent": "query_pnl", "params": {}, "response": ""}
「BTC 多少？」→ {"intent": "query_market", "params": {"symbol": "BTCUSDT"}, "response": ""}

# 系統操作
「暫停」→ {"intent": "action_pause", "params": {}, "response": ""}
「把槓桿改成 5」→ {"intent": "action_change_leverage", "params": {"leverage": 5}, "response": ""}
「加 ETH 進去做多」→ {"intent": "action_add_pair", "params": {"pair": "ETHUSDT", "type": "long"}, "response": ""}

# 市場分析（給有深度的回答，5-10 行）
「分析 BTC 走勢」→ {"intent": "chat", "params": {}, "response": "📊 BTC 走勢分析

🔹 技術面：
- 4 年週期看，2024 減半後通常進入主升段
- 留意關鍵支撐位的多空轉換
- KDJ、RSI 是觀察短期超買超賣的好工具

🔹 基本面：
- 機構 ETF 持續流入是主要動能
- 鏈上活躍地址、礦工持有量值得追蹤

🔹 風險：
- 聯準會利率政策變動
- 地緣政治和監管不確定性

💡 建議分批進場、嚴守止損，單筆部位不超過總資金 5%。

⚠️ 以上不構成投資建議，請自行判斷風險。"}

「現在能進場嗎」→ {"intent": "chat", "params": {}, "response": "📊 進場前 4 個檢查清單

1️⃣ 趨勢：日線是否在 200 日 MA 上方？順勢交易勝率高
2️⃣ 風報比：止損和止盈設置是否大於 1:2？
3️⃣ 倉位：是否超過總資金 5%？大部位風險高
4️⃣ 心態：是因為 FOMO 還是有計畫進場？

💡 建議：先看「狀態」確認 5 Agent 投票，作為進場依據。

⚠️ 提醒：再好的進場機會，沒有風控都會虧錢。"}

# 技術指標教學
「什麼是 RSI」→ {"intent": "chat", "params": {}, "response": "📚 RSI（相對強弱指標）

🔹 計算原理：14 天內漲跌幅比例
🔹 數值範圍：0-100

📊 訊號解讀：
- RSI > 70：超買，可能回檔
- RSI < 30：超賣，可能反彈
- RSI 50：多空平衡

⚠️ 注意：強勢趨勢中 RSI 可長時間維持高位/低位，不要單看 RSI 進出場。

💡 你的 Technical Agent 就是用 RSI 做判斷之一！"}

# 概念解釋
「什麼是 DeFi」→ {"intent": "chat", "params": {}, "response": "🔗 DeFi（去中心化金融）

簡單說：用區塊鏈代替銀行的金融服務

包含：
- 借貸（Aave、Compound）
- 交易（Uniswap）
- 穩定幣（DAI）
- 收益農場

優點：透明、開放、無需審核
風險：智能合約漏洞、無人客服、高 Gas 費"}

# 一般對話
「給我講個笑話」→ {"intent": "chat", "params": {}, "response": "為什麼比特幣去看心理醫生？因為它有「分叉」型人格 😂"}

「我虧錢心情很差」→ {"intent": "chat", "params": {}, "response": "聽到你虧錢，我能理解這種心情有多難受 💛

虧損是交易必經之路，連巴菲特和索羅斯都有過大幅虧損。

幾個建議：
1️⃣ 先停下來，不要急著扳回
2️⃣ 檢視虧損原因：是策略錯，還是執行錯？
3️⃣ 縮小部位繼續交易，找回手感
4️⃣ 必要時休息一週，整理心態

要聊聊嗎？"}

【回應風格】
- 繁體中文，台灣用語（不要簡體）
- 親切但專業，像個會交易的朋友
- 適當 emoji（不過量，每段 1-2 個）
- 用 
 換行讓訊息易讀
- 分析類回答有深度（5-10 行）
- 操作類回答簡短（讓系統實際執行）
- 強調風險、不給絕對建議

【重要原則】
- 你不是金融顧問，避免「一定漲」「保證賺」這種絕對性語言
- 鼓勵使用者自行判斷
- 強調風險控制和倉位管理
- 只輸出 JSON，無其他文字"""


def parse_user_intent(text: str) -> dict:
    """用 Claude 解析使用者意圖"""
    if not anthropic_client:
        return {'intent': 'chat', 'response': '⚠️ AI 服務未設定（缺少 ANTHROPIC_API_KEY）'}
    
    try:
        resp = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": text},
            ],
        )
        # Claude 回傳的內容在 content[0].text
        content = resp.content[0].text.strip()
        
        # 移除可能的 markdown code block (```json ... ```)
        if content.startswith('```'):
            lines = content.split('\n')
            content = '\n'.join(lines[1:-1] if lines[-1].startswith('```') else lines[1:])
        
        result = json.loads(content)
        
        # 如果 Claude 回傳的是陣列（多個指令），合併成一個回應
        if isinstance(result, list):
            if len(result) == 0:
                return {'intent': 'chat', 'response': '😅 我沒有理解你的意思'}
            
            # 如果只有一個元素，直接返回
            if len(result) == 1:
                return result[0]
            
            # 多個元素：合併成單一回應
            # 處理每個意圖，把所有 chat 類的 response 串起來
            combined_responses = []
            for item in result:
                intent = item.get('intent', 'chat')
                if intent == 'chat':
                    # 純對話：把 response 加入合併文字
                    if item.get('response'):
                        combined_responses.append(item['response'])
                else:
                    # 有實際操作的意圖：標註但不執行（避免一次執行多個操作）
                    combined_responses.append(f"⚠️ 偵測到操作指令「{intent}」，請單獨傳送以執行。")
            
            return {
                'intent': 'chat',
                'params': {},
                'response': '\n\n━━━━━━━━━━━━━━━\n\n'.join(combined_responses)
            }
        
        return result
    except json.JSONDecodeError as e:
        logger.error(f"Claude JSON parse error: {e}, content: {content[:200] if 'content' in dir() else 'N/A'}")
        # JSON 解析失敗就把整段當作 chat 回應
        return {'intent': 'chat', 'response': content if 'content' in dir() else '😅 我有點困惑'}
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return {'intent': 'chat', 'response': f'😅 系統錯誤：{str(e)[:100]}'}


# ============================================================================
# 意圖處理器
# ============================================================================

def handle_query_status() -> str:
    config = get_bot_config()
    balance = get_binance_balance()
    today_pnl = get_today_pnl()
    
    enabled = "🟢 運行中" if config.get('isEnabled') else "🔴 已暫停"
    pairs = config.get('monitorPairs', [])
    if isinstance(pairs, str):
        pairs = json.loads(pairs)
    
    return f"""📊 系統狀態

{enabled}
🤖 監控 {len(pairs)} 個交易對
💰 合約餘額：${balance['futures_balance']:.2f} USDT
✅ 可用餘額：${balance['futures_available']:.2f} USDT
📈 今日盈虧：${today_pnl:+.2f}
⚙️ 槓桿：{config.get('leverage', 3)}x
🎯 單筆上限：${float(config.get('positionSizeMax', 0.5)):.2f}"""


def handle_query_trades(params: dict) -> str:
    limit = params.get('limit', 5)
    trades = get_recent_trades(limit)
    
    if not trades:
        return "📋 目前還沒有交易記錄"
    
    lines = [f"📋 最近 {len(trades)} 筆交易:\n"]
    for t in trades:
        pnl = float(t.get('pnl') or 0)
        emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        side = t.get('side', '?')
        symbol = t.get('symbol', '?')
        qty = float(t.get('quantity', 0))
        price = float(t.get('price', 0))
        time_str = t['createdAt'].strftime('%m/%d %H:%M') if t.get('createdAt') else '?'
        lines.append(f"{emoji} {side} {symbol}\n   {qty:.4f} @ ${price:.4f} | PnL: ${pnl:+.2f} | {time_str}")
    
    return "\n\n".join(lines)


def handle_query_config() -> str:
    config = get_bot_config()
    
    pairs = config.get('monitorPairs', [])
    longs = config.get('longPairs', [])
    shorts = config.get('shortPairs', [])
    if isinstance(pairs, str): pairs = json.loads(pairs)
    if isinstance(longs, str): longs = json.loads(longs)
    if isinstance(shorts, str): shorts = json.loads(shorts)
    
    return f"""⚙️ 機器人設定

💰 資金管理
• 初始資金：${float(config.get('initialUsdt', 5)):.2f}
• 單筆上限：${float(config.get('positionSizeMax', 0.5)):.2f}
• 槓桿：{config.get('leverage', 3)}x
• 每日虧損上限：${float(config.get('dailyLossLimit', 5)):.2f}

🎯 平倉條件
• 止損：{config.get('stopLoss', -35)}%
• 止盈 1：{config.get('profitTarget1', 20)}%
• 止盈 2：{config.get('profitTarget2', 25)}%

📊 監控交易對
• 主力：{', '.join(pairs) or '無'}
• 做多：{', '.join(longs) or '無'}
• 做空：{', '.join(shorts) or '無'}

⏱️ 檢查頻率：{config.get('checkIntervalSeconds', 60)} 秒"""


def handle_query_pnl() -> str:
    today_pnl = get_today_pnl()
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT SUM(pnl) as total, COUNT(*) as count FROM trades WHERE userId = %s",
                (USER_ID,)
            )
            row = cursor.fetchone()
            total_pnl = float(row['total'] or 0)
            total_count = row['count'] or 0
    finally:
        conn.close()
    
    emoji = "🟢" if total_pnl > 0 else "🔴" if total_pnl < 0 else "⚪"
    today_emoji = "🟢" if today_pnl > 0 else "🔴" if today_pnl < 0 else "⚪"
    
    return f"""📈 盈虧報告

{today_emoji} 今日：${today_pnl:+.2f}
{emoji} 總計：${total_pnl:+.2f}
📊 總交易：{total_count} 筆"""


def handle_query_market(params: dict) -> str:
    symbol = params.get('symbol', 'BTCUSDT').upper()
    if not symbol.endswith('USDT'):
        symbol += 'USDT'
    
    try:
        resp = requests.get(
            f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}",
            timeout=5
        )
        if resp.status_code != 200:
            return f"❌ 找不到 {symbol}"
        
        data = resp.json()
        price = float(data.get('lastPrice', 0))
        change_pct = float(data.get('priceChangePercent', 0))
        high = float(data.get('highPrice', 0))
        low = float(data.get('lowPrice', 0))
        volume = float(data.get('quoteVolume', 0))
        
        emoji = "🟢" if change_pct > 0 else "🔴" if change_pct < 0 else "⚪"
        
        return f"""📊 {symbol}

💵 現價：${price:,.4f}
{emoji} 24h 漲跌：{change_pct:+.2f}%
📈 24h 高：${high:,.4f}
📉 24h 低：${low:,.4f}
💰 24h 成交量：${volume:,.0f}"""
    except Exception as e:
        return f"⚠️ 查詢失敗：{e}"


def handle_action_pause() -> str:
    if update_bot_config('isEnabled', False):
        return "⏸️ 機器人已暫停\n\n所有監控仍會持續，但不會執行新交易。\n說「恢復」可重新啟動。"
    return "❌ 暫停失敗"


def handle_action_resume() -> str:
    if update_bot_config('isEnabled', True):
        return "▶️ 機器人已恢復\n\n下次循環將開始執行交易。"
    return "❌ 恢復失敗"


def handle_action_change_leverage(params: dict) -> str:
    leverage = int(params.get('leverage', 3))
    if leverage < 1 or leverage > 50:
        return "❌ 槓桿必須在 1-50 之間"
    if update_bot_config('leverage', leverage):
        return f"✅ 槓桿已調整為 {leverage}x\n\n⚠️ 提醒：高槓桿風險極高，請謹慎"
    return "❌ 調整失敗"


def handle_action_add_pair(params: dict) -> str:
    pair = params.get('pair', '').upper()
    pair_type = params.get('type', 'long')  # long/short/main
    
    if not pair:
        return "❌ 請告訴我要新增哪個交易對"
    
    if not pair.endswith('USDT'):
        pair += 'USDT'
    
    field_map = {
        'main': 'monitorPairs',
        'long': 'longPairs',
        'short': 'shortPairs',
    }
    field = field_map.get(pair_type, 'longPairs')
    
    config = get_bot_config()
    current = config.get(field, [])
    if isinstance(current, str):
        current = json.loads(current)
    
    if pair in current:
        return f"⚠️ {pair} 已經在監控列表"
    
    current.append(pair)
    if update_bot_config(field, current):
        type_name = {'main': '主力', 'long': '做多', 'short': '做空'}[pair_type]
        return f"✅ 已新增 {pair} 至「{type_name}」監控\n\n目前監控 {len(current)} 個{type_name}交易對"
    return "❌ 新增失敗"


def handle_action_remove_pair(params: dict) -> str:
    pair = params.get('pair', '').upper()
    if not pair.endswith('USDT'):
        pair += 'USDT'
    
    config = get_bot_config()
    removed = []
    
    for field in ['monitorPairs', 'longPairs', 'shortPairs']:
        current = config.get(field, [])
        if isinstance(current, str):
            current = json.loads(current)
        if pair in current:
            current.remove(pair)
            update_bot_config(field, current)
            removed.append(field)
    
    if removed:
        return f"✅ 已移除 {pair}"
    return f"❌ {pair} 不在監控列表"


# ============================================================================
# 主處理邏輯
# ============================================================================

INTENT_HANDLERS = {
    'query_status': lambda p: handle_query_status(),
    'query_trades': handle_query_trades,
    'query_config': lambda p: handle_query_config(),
    'query_pnl': lambda p: handle_query_pnl(),
    'query_market': handle_query_market,
    'action_pause': lambda p: handle_action_pause(),
    'action_resume': lambda p: handle_action_resume(),
    'action_change_leverage': handle_action_change_leverage,
    'action_add_pair': handle_action_add_pair,
    'action_remove_pair': handle_action_remove_pair,
}


def process_message(text: str, user_id: str) -> str:
    """處理使用者訊息，回傳要回覆的文字"""
    
    # 安全檢查：只允許白名單使用者
    if ALLOWED_USER_IDS and ALLOWED_USER_IDS != [''] and user_id not in ALLOWED_USER_IDS:
        return "🚫 抱歉，你沒有使用此 Bot 的權限"
    
    # 簡單指令直接處理（不浪費 GPT API 額度）
    text_lower = text.lower().strip()
    quick_commands = {
        '/status': lambda: handle_query_status(),
        '狀態': lambda: handle_query_status(),
        '/pause': lambda: handle_action_pause(),
        '暫停': lambda: handle_action_pause(),
        '/resume': lambda: handle_action_resume(),
        '恢復': lambda: handle_action_resume(),
        '/trades': lambda: handle_query_trades({'limit': 5}),
        '交易': lambda: handle_query_trades({'limit': 5}),
        '/pnl': lambda: handle_query_pnl(),
        '盈虧': lambda: handle_query_pnl(),
        '/config': lambda: handle_query_config(),
        '設定': lambda: handle_query_config(),
        '/help': lambda: get_help_text(),
        '說明': lambda: get_help_text(),
    }
    
    if text_lower in quick_commands:
        return quick_commands[text_lower]()
    
    # 否則用 GPT 解析
    parsed = parse_user_intent(text)
    intent = parsed.get('intent', 'chat')
    params = parsed.get('params', {})
    
    if intent == 'chat':
        return parsed.get('response', '...')
    
    handler = INTENT_HANDLERS.get(intent)
    if handler:
        return handler(params)
    
    return parsed.get('response', '😅 我沒有完全理解，能試著用其他方式說嗎？')


def get_help_text() -> str:
    return """🤖 CryptoBot AI 使用說明

📋 快速指令
• 狀態 / /status - 查看系統狀態
• 交易 / /trades - 最近交易記錄
• 盈虧 / /pnl - 盈虧報告
• 設定 / /config - 機器人設定
• 暫停 / /pause - 暫停機器人
• 恢復 / /resume - 恢復機器人

💬 自然對話（AI 模式）
你也可以用自然語言：
• "BTC 現在多少？"
• "我今天賺多少？"
• "把槓桿改成 5 倍"
• "加 ETH 進去監控"
• "幫我分析一下市場"

🔒 所有指令都需要你是授權使用者"""


# ============================================================================
# Webhook 端點
# ============================================================================

@app.post("/webhook")
async def line_webhook(request: Request, background_tasks: BackgroundTasks):
    """LINE Webhook 接收端點"""
    body = await request.body()
    signature = request.headers.get('X-Line-Signature', '')
    
    # 簽章驗證
    if not verify_signature(body, signature):
        logger.warning("Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    payload = json.loads(body)
    events = payload.get('events', [])
    
    for event in events:
        event_type = event.get('type')
        
        # 文字訊息
        if event_type == 'message' and event['message']['type'] == 'text':
            user_id = event['source'].get('userId', '')
            text = event['message']['text']
            reply_token = event['replyToken']
            
            logger.info(f"Message from {user_id}: {text}")
            
            # 背景處理（避免 LINE timeout）
            background_tasks.add_task(handle_text_message, user_id, text, reply_token)
        
        # Postback（按鈕點擊）
        elif event_type == 'postback':
            user_id = event['source'].get('userId', '')
            data = event['postback']['data']
            reply_token = event['replyToken']
            
            background_tasks.add_task(handle_postback, user_id, data, reply_token)
    
    return {"status": "ok"}


def handle_text_message(user_id: str, text: str, reply_token: str):
    """處理文字訊息"""
    try:
        response = process_message(text, user_id)
        line_reply(reply_token, [text_message(response)])
    except Exception as e:
        logger.error(f"Handle message error: {e}", exc_info=True)
        line_reply(reply_token, [text_message(f"😅 系統錯誤：{e}")])


def handle_postback(user_id: str, data: str, reply_token: str):
    """處理按鈕點擊（用於確認交易）"""
    try:
        if data.startswith('confirm_trade:'):
            trade_id = data.split(':')[1]
            # TODO: 標記該筆交易為已確認
            line_reply(reply_token, [text_message(f"✅ 已確認交易 {trade_id}")])
        elif data.startswith('cancel_trade:'):
            trade_id = data.split(':')[1]
            line_reply(reply_token, [text_message(f"❌ 已取消交易 {trade_id}")])
    except Exception as e:
        logger.error(f"Postback error: {e}")


# ============================================================================
# 健康檢查
# ============================================================================

@app.get("/")
def root():
    return {
        "service": "CryptoBot LINE AI (Claude)",
        "status": "running",
        "anthropic": bool(ANTHROPIC_API_KEY),
        "line": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "database": bool(DATABASE_URL),
        "model": CLAUDE_MODEL,
    }


# ============================================================================
# 提供給 trading_bot.py 使用的「主動推送」函數
# ============================================================================

def push_trade_notification(user_id: str, trade_info: dict):
    """交易通知（給主程式呼叫）"""
    msg = f"""🔔 交易執行

{trade_info.get('side', '?')} {trade_info.get('symbol', '?')}
數量：{trade_info.get('quantity', 0):.4f}
價格：${trade_info.get('price', 0):.4f}
時間：{datetime.now().strftime('%H:%M:%S')}"""
    line_push(user_id, [text_message(msg)])


def push_large_order_confirm(user_id: str, trade_info: dict, trade_id: str):
    """大額交易確認（按鈕）"""
    text = f"⚠️ 大額交易確認\n\n{trade_info['side']} {trade_info['symbol']} ${trade_info['amount']:.2f}\n槓桿 {trade_info['leverage']}x\n\n是否執行？"
    line_push(user_id, [
        confirm_message(
            text,
            f"confirm_trade:{trade_id}",
            f"cancel_trade:{trade_id}"
        )
    ])


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv('PORT', 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
