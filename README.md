# Vietnamese Stock AI Agent

Hệ thống AI Agent nhiều module phân tích và đưa ra khuyến nghị giao dịch cho thị trường chứng khoán Việt Nam.

## Kiến trúc Pipeline

```
Data Engine → TA Engine → Risk Engine → FA Engine → Sentiment Engine → Agent Brain → Telegram Bot
```

| Module | File | Chức năng |
|---|---|---|
| Data Engine | `data_engine.py` | Thu thập giá OHLCV real-time qua vnstock, chạy nền 24/7 |
| Backfill | `backfill_lich_su.py` | Tải lịch sử giá 1 lần cho toàn bộ watchlist |
| TA Engine | `ta_engine.py` | Chỉ báo kỹ thuật (RSI/MACD/EMA/BBands), hỗ trợ/kháng cự, tín hiệu multi-factor |
| Risk Engine | `risk_engine.py` | Entry/Stop Loss/Take Profit, biên độ giao dịch HOSE, rule T+2.5, Beta/Max Drawdown |
| FA Engine | `fa_engine.py` | Chỉ số tài chính (ROE, ROA, P/E...) tự tính từ báo cáo tài chính thô |
| Sentiment Engine | `sentiment_engine.py` | Phân tích tâm lý thị trường từ tin tức CafeF qua Gemini AI |
| Agent Brain | `agent_brain.py` | Tổng hợp toàn bộ dữ liệu, đưa ra quyết định BUY/SELL/HOLD qua Gemini AI |
| Telegram Bot | `bot_telegram.py` | Gửi cảnh báo tự động khi có tín hiệu BUY/SELL |
| Main | `main.py` | File trung tâm điều phối toàn bộ pipeline, lưu kết quả ra Excel |

## Công nghệ sử dụng

- **Python 3.13**
- **vnstock** - dữ liệu chứng khoán Việt Nam
- **pandas / pandas_ta** - xử lý dữ liệu & chỉ báo kỹ thuật
- **Google Gemini AI** (`google-genai`) - tổng hợp phân tích & sentiment
- **pydantic** - validate output có cấu trúc từ AI
- **openpyxl** - xuất kết quả ra Excel nhiều sheet
- **python-telegram-bot API** - cảnh báo tự động

## Cài đặt

```bash
pip install vnstock pandas pandas_ta google-genai pydantic python-dotenv openpyxl requests
```

Tạo file `.env` trong thư mục gốc với nội dung:
```
GEMINI_API_KEY=your_gemini_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

## Sử dụng

```bash
# Tải dữ liệu lịch sử lần đầu
python backfill_lich_su.py

# Chạy phân tích toàn bộ watchlist
python main.py

# (Tuỳ chọn) Chạy Data Engine nền 24/7 để tự động cập nhật giá
python data_engine.py
```

## Lưu ý

Đây là dự án phục vụ mục đích học tập/nghiên cứu, không phải khuyến nghị đầu tư tài chính chính thức.
