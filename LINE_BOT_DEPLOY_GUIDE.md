# 🤖 LINE Bot AI 部署完整指南

## 📋 前置作業 Checklist

- [ ] LINE 個人帳號（你已有）
- [ ] LINE 開發者帳號（你已有）
- [ ] OpenAI API Key（你已有 ChatGPT API Key）
- [ ] Zeabur 帳號 + Python 服務（你已有）

---

## 🔧 步驟 1：建立 LINE Bot Channel

1. 前往 https://developers.line.biz/console/
2. 選一個 Provider → **Create new channel**
3. 選 **Messaging API**
4. 填寫資訊（隨意）→ Create

### 取得 2 個關鍵 Token

進入剛建立的 Channel：

#### A. Channel Access Token
路徑：**Messaging API** 標籤 → 最下方 **Channel access token (long-lived)** → 點 **Issue**

複製這串很長的 token（記為 `LINE_CHANNEL_ACCESS_TOKEN`）

#### B. Channel Secret
路徑：**Basic settings** 標籤 → **Channel secret**

複製（記為 `LINE_CHANNEL_SECRET`）

#### C. 加 Bot 為好友
**Messaging API** 標籤 → **QR code** → 用手機 LINE 掃 → 加為好友

---

## 🚀 步驟 2：在 Zeabur 部署 line_bot.py

### 方案 A：新增獨立 Python 服務（推薦）

1. Zeabur Dashboard → 你的專案 → **建立服務**
2. 選 **GitHub Repo** → 選 `autotrade` repo
3. 服務名稱：`line-bot`
4. 上傳 `line_bot.py` 到 GitHub repo（可放在新資料夾 `line_bot/` 或根目錄）
5. 確保 `requirements.txt` 有：
   ```
   requests>=2.31.0
   PyMySQL>=1.1.0
   fastapi>=0.110.0
   uvicorn>=0.29.0
   openai>=1.30.0
   ```

6. 設定 **Start Command**：
   ```
   python line_bot.py
   ```

### 方案 B：合併到現有 Python 服務

把 line_bot.py 的內容**合併到** trading_bot.py，但需要用 multiprocessing 同時跑 webhook 和交易迴圈（較複雜）。

**建議用方案 A**！

---

## 🔑 步驟 3：設定 Zeabur 環境變數

進入 line-bot 服務的 **環境變數** 標籤，加入：

| 變數名 | 值 | 說明 |
|--------|---|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | （從 LINE 複製的）| Bot 推送用 |
| `LINE_CHANNEL_SECRET` | （從 LINE 複製的）| Webhook 簽章驗證 |
| `OPENAI_API_KEY` | `sk-...` | 你的 ChatGPT API Key |
| `GPT_MODEL` | `gpt-4o-mini` | 便宜快速（也可改 `gpt-4o`）|
| `DATABASE_URL` | `mysql://root:xxx@service-xxx:3306/zeabur` | 同 trading_bot 的設定 |
| `USER_ID` | `1` | 同 trading_bot 的設定 |
| `BINANCE_API_KEY` | （你的幣安 API Key）| 查餘額用 |
| `BINANCE_API_SECRET` | （你的幣安 Secret）| 查餘額用 |
| `LINE_ALLOWED_USER_IDS` | （見下方說明）| **重要：白名單** |
| `PORT` | `8000` | FastAPI 預設 port |

---

## 🌐 步驟 4：取得 Webhook URL

部署完成後：

1. Zeabur → line-bot 服務 → **網域** → 取得網址
   - 例如：`line-bot-xxxxx.zeabur.app`

2. Webhook URL 為：
   ```
   https://line-bot-xxxxx.zeabur.app/webhook
   ```

3. 回到 LINE 開發者後台 → 你的 Channel → **Messaging API** 標籤
4. **Webhook URL** 填入上方網址 → **Update**
5. **Use webhook** → **開啟**
6. 點 **Verify** → 應該顯示 ✅ **Success**

---

## 🔒 步驟 5：取得你的 LINE User ID（白名單設定）

**這是最重要的安全步驟！** 不設白名單，任何加你 Bot 的人都能控制你的交易！

### 取得方法 1：用 LINE Bot 自己看

1. **暫時不設定** `LINE_ALLOWED_USER_IDS`（留空）
2. 部署完後，在 LINE 對 Bot 說「test」
3. 到 Zeabur → line-bot 服務 → **記錄**
4. 找這行 log：
   ```
   Message from U1234567890abcdef...: test
   ```
5. 複製這串 `U` 開頭的字串（這就是你的 LINE User ID）

### 取得方法 2：用 LINE 後台

LINE Console → 你的 Channel → **Basic settings** → **Your user ID**（如果有）

### 設定白名單

回到 Zeabur 環境變數，設定：
```
LINE_ALLOWED_USER_IDS=U1234567890abcdef...
```

如果有多個使用者：
```
LINE_ALLOWED_USER_IDS=U123abc...,U456def...
```

重新部署！

---

## ✅ 步驟 6：測試！

打開你的 LINE，找到剛加的 Bot，試著傳這些訊息：

### 快速指令測試
```
你: 狀態
Bot: 📊 系統狀態
     🟢 運行中
     🤖 監控 7 個交易對
     💰 合約餘額：$0.00 USDT
     ...
```

```
你: 盈虧
Bot: 📈 盈虧報告
     ⚪ 今日：$0.00
     ...
```

### AI 對話測試
```
你: BTC 現在多少？
Bot: 📊 BTCUSDT
     💵 現價：$78,381.00
     🟢 24h 漲跌：+2.49%
     ...
```

```
你: 把槓桿改成 5 倍
Bot: ✅ 槓桿已調整為 5x
     ⚠️ 提醒：高槓桿風險極高，請謹慎
```

```
你: 給我市場分析
Bot: 🤖 [GPT 自然語言回答市場狀況]
```

---

## 💡 進階：讓 trading_bot.py 主動推送通知

要讓 trading_bot.py 在執行交易時也透過 LINE 通知，需要在 `trading_bot.py` 加入：

```python
import requests

def send_line_message(user_id: str, message: str):
    """主動推送 LINE 訊息"""
    headers = {
        'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
        'Content-Type': 'application/json',
    }
    payload = {
        'to': user_id,
        'messages': [{'type': 'text', 'text': message}],
    }
    requests.post('https://api.line.me/v2/bot/message/push', headers=headers, json=payload)

# 在交易執行時呼叫
send_line_message(YOUR_USER_ID, f"✅ 已買入 {symbol} @ ${price}")
```

需要 trading_bot.py 也加入環境變數：
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_USER_ID`（接收通知的人）

---

## 💰 OpenAI 成本估算

使用 `gpt-4o-mini` 每次對話約 $0.0001 USD（**幾乎免費**）

每月 1000 次對話 ≈ $0.10 USD（3 元台幣）

**如果想更省**：可以加快取，常見問題不打 GPT。

---

## 🐛 除錯

### 問題：Webhook Verify 失敗
- 檢查 URL 是否正確（有 https + /webhook）
- 檢查 `LINE_CHANNEL_SECRET` 是否正確
- 看 Zeabur log 有沒有錯誤

### 問題：Bot 不回覆
- 確認 **Use webhook** 是開啟的
- 確認 LINE Console 的「自動回覆訊息」是**關閉**的（會搶 webhook）
- 確認你的 User ID 在 `LINE_ALLOWED_USER_IDS` 裡

### 問題：GPT 回答怪怪的
- 試試改 `GPT_MODEL=gpt-4o`（更聰明但貴 5 倍）
- 或調整 system prompt（在 line_bot.py 的 `SYSTEM_PROMPT` 變數）

### 問題：MySQL 連不上
- 檢查 `DATABASE_URL` 是否和 trading_bot.py 用的一樣
- Zeabur 的 MySQL 服務和 line-bot 服務要在**同一個 Project**

---

## 🎯 完成後你能做什麼？

✅ **查詢類**
- 「狀態」「盈虧」「設定」「交易」
- 「BTC 現在多少？」
- 「我今天賺多少？」

✅ **操作類**
- 「停止機器人」「恢復機器人」
- 「把槓桿改成 5 倍」
- 「加 ETH 進去監控」
- 「移除 DOGE」

✅ **AI 對話**
- 「給我市場分析」
- 「現在該買 ETH 嗎？」
- 「為什麼今天沒進場？」

✅ **24/7 即時通知**
- 每筆交易執行立即推送
- 觸發止損/止盈即時通知
- 系統異常告警

---

## 🎁 額外功能（未來可加）

- [ ] 大額交易確認按鈕（按鈕介面）
- [ ] 圖表報告（Flex Message）
- [ ] 每日績效自動報告（cron）
- [ ] Agent 投票結果即時推送
- [ ] 多人共用 Bot（需要更複雜的權限）

需要哪個告訴我！
