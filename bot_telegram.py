"""
bot_telegram.py
=================
Module cuối: gửi cảnh báo qua Telegram khi có tín hiệu BUY/SELL đáng chú ý.
KHÔNG gửi khi action = HOLD, tránh spam tin nhắn không cần thiết.

CHUẨN BỊ TRƯỚC KHI DÙNG (đã hướng dẫn riêng):
1. Tạo bot qua @BotFather trên Telegram -> lấy Bot Token
2. Lấy Chat ID qua @userinfobot
3. Bấm Start / nhắn tin cho chính bot của mình trước (bắt buộc, nếu không bot
   sẽ không gửi được tin dù đúng Chat ID)
4. Thêm 2 dòng vào file .env (cùng thư mục, nơi đã có GEMINI_API_KEY):
   TELEGRAM_BOT_TOKEN=123456789:ABCdef...
   TELEGRAM_CHAT_ID=123456789
"""

import os
import logging

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bot_telegram")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def dinh_dang_tin_nhan(quyet_dinh, ma: str) -> str:
    """
    Định dạng 1 QuyetDinhGiaoDich (từ agent_brain.py) thành tin nhắn Telegram
    dễ đọc, dùng Markdown của Telegram (*in đậm*, không phải Markdown chuẩn).
    """
    bieu_tuong = "🟢 MUA" if quyet_dinh.action == "BUY" else "🔴 BÁN"

    dong_gia = ""
    if quyet_dinh.entry_price is not None:
        dong_gia = (
            f"\n💰 Entry: *{quyet_dinh.entry_price}*"
            f"\n🛑 Stop Loss: *{quyet_dinh.stop_loss}*"
            f"\n🎯 Take Profit: *{quyet_dinh.target_price}*"
        )

    return (
        f"{bieu_tuong} *{ma}*\n"
        f"Độ tin cậy: *{quyet_dinh.confidence_score}/100* | Rủi ro: *{quyet_dinh.risk_level}*"
        f"{dong_gia}\n\n"
        f"📊 *Kỹ thuật:* {quyet_dinh.analysis_summary.technical}\n"
        f"📰 *Tâm lý:* {quyet_dinh.analysis_summary.sentiment}\n"
        f"💵 *Dòng tiền:* {quyet_dinh.analysis_summary.money_flow}"
    )


def gui_tin_nhan(noi_dung: str, max_retries: int = 3) -> bool:
    """Gửi 1 tin nhắn text tới Telegram, tự retry nếu lỗi mạng tạm thời."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Chưa cấu hình TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID trong file .env - không gửi được.")
        return False

    payload = {"chat_id": CHAT_ID, "text": noi_dung, "parse_mode": "Markdown"}

    for attempt in range(max_retries):
        try:
            response = requests.post(TELEGRAM_API_URL, data=payload, timeout=10)
            if response.status_code == 200:
                return True
            logger.warning(f"Telegram trả về lỗi (lần {attempt + 1}/{max_retries}): {response.status_code} - {response.text}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Lỗi mạng khi gửi Telegram (lần {attempt + 1}/{max_retries}): {e}")

    logger.error("Không gửi được tin nhắn Telegram sau nhiều lần thử.")
    return False


def gui_canh_bao_neu_can(quyet_dinh, ma: str) -> bool:
    """
    Chỉ gửi cảnh báo khi action là BUY hoặc SELL - CHỦ ĐỘNG BỎ QUA HOLD để
    tránh spam (theo yêu cầu: chỉ báo tín hiệu đáng chú ý).
    Trả về True nếu đã gửi (hoặc không cần gửi), False nếu gửi thất bại.
    """
    if quyet_dinh is None:
        return True  # không có quyết định hợp lệ thì không có gì để gửi, không tính là lỗi

    if quyet_dinh.action == "HOLD":
        logger.info(f"[{ma}] Tín hiệu HOLD - không gửi Telegram (tránh spam).")
        return True

    noi_dung = dinh_dang_tin_nhan(quyet_dinh, ma)
    thanh_cong = gui_tin_nhan(noi_dung)
    if thanh_cong:
        logger.info(f"[{ma}] Đã gửi cảnh báo {quyet_dinh.action} qua Telegram.")
    return thanh_cong


# --- TEST NHANH ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    ok = gui_tin_nhan("🤖 Test kết nối Bot Telegram từ đồ án Zero Trust Stock Agent - nếu thấy tin này là đã setup đúng!")
    if ok:
        print("✅ Gửi thành công - kiểm tra Telegram xem đã nhận được tin chưa.")
    else:
        print("❌ Gửi thất bại - kiểm tra lại TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID trong .env, và đã bấm Start với bot chưa.")
