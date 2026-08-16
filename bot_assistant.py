# -*- coding: utf-8 -*-
"""
bot_assistant.py
=================
FILE DUY NHẤT CẦN CHẠY - gộp toàn bộ các việc phải chạy riêng trước đây
(bot_assistant.py, data_engine.py, main.py) vào CHUNG 1 tiến trình, dùng
`apscheduler` (BackgroundScheduler, chạy job trong thread nền riêng nên
KHÔNG chặn vòng lặp nghe Telegram):

  1. NGHE CHAT (Telegram polling)      - trả lời tức thì, tự khởi động lại
     nếu mạng rớt (xem hàm main() ở cuối file)
  2. FETCH GIÁ TỰ ĐỘNG mỗi 30 phút     - logic mượn nguyên từ data_engine.py
  3. CHẠY PIPELINE PHÂN TÍCH TỰ ĐỘNG   - logic mượn nguyên từ main.py,
     2 lần/ngày: 11:35 (nghỉ trưa) và 15:15 (đóng cửa). Nếu bot khởi động
     MUỘN và đã lỡ hết các mốc giờ trong ngày mà hôm đó CHƯA có lần phân
     tích nào -> tự động CHẠY BÙ 1 lần duy nhất ngay sau khi khởi động
     (xem kiem_tra_va_bu_pipeline_neu_can()).
  4. THEO DÕI DANH MỤC CÁ NHÂN         - người dùng tự khai báo vị thế qua
     /muavao (KHÔNG kết nối tài khoản chứng khoán thật nào cả). SL/TP mặc
     định tự tính theo % CỐ ĐỊNH trên giá vào (lỗ 3.5% -> cắt lỗ, lãi 7% ->
     chốt lời - chỉnh ở NGUONG_CAT_LO_PCT/NGUONG_CHOT_LOI_PCT bên dưới),
     không phụ thuộc risk_engine/phân tích kỹ thuật nên LUÔN tính được, kể
     cả với mã đang HOLD hoặc chưa từng được phân tích. Vẫn có thể nhập tay
     SL/TP cụ thể nếu muốn override. Bot tự so giá mới nhất với SL/TP mỗi
     30 phút (ngay sau khi fetch giá xong) và tự cảnh báo qua Telegram khi
     chạm ngưỡng.

data_engine.py và main.py GIỮ NGUYÊN không đổi cấu trúc - chỉ đóng vai trò
"thư viện" chứa logic. bot_telegram.py cũng giữ nguyên, dùng chung cho cả
cảnh báo BUY/SELL (qua main.py) lẫn cảnh báo chạm SL/TP (qua file này).

backfill_lich_su.py KHÔNG nằm trong file này - script chạy 1 LẦN DUY NHẤT
khi cần backfill lịch sử ban đầu, vẫn chạy tay riêng khi cần.

CHẠY (chỉ cần 1 lệnh duy nhất):
  python bot_assistant.py
"""

import os
import re
import sys
import logging
import unicodedata
import time
from datetime import datetime, timedelta

import pandas as pd
import openpyxl
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from apscheduler.schedulers.background import BackgroundScheduler
from vnstock.ui import Market

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_engine
import main as pipeline  # main.py - chỉ dùng như thư viện, KHÔNG tự chạy nhờ if __name__ guard của nó
import bot_telegram       # dùng lại hàm gui_tin_nhan() có sẵn cho cảnh báo chạm SL/TP

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

# ==================== CẤU HÌNH ====================
CSV_GIA_PATH = r"C:\vn_stock_agent_data\lich_su_gia.csv"
XLSX_PATH = r"C:\vn_stock_agent_data\phan_tich_ky_thuat.xlsx"
DANH_MUC_PATH = r"C:\vn_stock_agent_data\danh_muc.csv"
LOG_FOLDER = r"C:\vn_stock_agent_data\logs"
LOG_PATH = os.path.join(LOG_FOLDER, "bot_assistant.log")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Giờ chạy pipeline phân tích tự động (giờ Việt Nam, 24h) - chỉnh ở đây nếu muốn đổi lịch.
GIO_CHAY_PIPELINE = [(11, 35), (15, 15)]

# Ngưỡng % lãi/lỗ để TỰ TÍNH SL/TP khi khai báo vị thế qua /muavao (áp dụng
# khi không nhập tay SL/TP cụ thể) - tính trực tiếp trên GIÁ VÀO, không phụ
# thuộc risk_engine/phân tích kỹ thuật nên luôn tính được cho mọi trường hợp.
NGUONG_CAT_LO_PCT = 3.5    # lỗ quá 3.5% so với giá vào -> cảnh báo cắt lỗ
NGUONG_CHOT_LOI_PCT = 7.0  # lãi quá 7.0% so với giá vào -> cảnh báo chốt lời

# Watchlist hiển thị cho /watchlist + dùng để nhận diện mã CP trong câu hỏi tự nhiên.
# Giữ đồng bộ thủ công với WATCHLIST trong main.py / data_engine.py.
WATCHLIST = [
    "VCB", "BID", "TCB", "VPB", "MBB", "ACB",
    "HDB", "VRE", "OCB", "LPB", "HPG", "FPT",
]

TU_KHOA_PHAN_TICH = [
    "phân tích", "phan tich", "tín hiệu", "tin hieu", "nên mua", "nen mua",
    "nên bán", "nen ban", "mua không", "mua khong", "bán không", "ban khong",
    "khuyến nghị", "khuyen nghi", "action", "buy", "sell",
]
TU_KHOA_GIA = [
    "giá", "gia", "bao nhiêu", "bao nhieu", "hôm nay", "hom nay",
    "thế nào", "the nao", "close", "đóng cửa", "dong cua",
]

KHOA_MA_GAN_NHAT = "ma_gan_nhat"  # key lưu trong context.user_data

KHOA_NGAY_GAN_NHAT = "ngay_tuong_tac_gan_nhat"
TEN_THU_TRONG_TUAN = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]

TU_KHOA_CHI_TIET = {
    "ky_thuat": ["kỹ thuật", "ky thuat", "rsi", "macd", "bollinger", "ema", "chỉ báo"],
    "co_ban": ["cơ bản", "co ban", " fa ", "pe ", "roe", "báo cáo tài chính", "bao cao tai chinh"],
    "tam_ly": ["tâm lý", "tam ly", "sentiment", "tin tức", "tin tuc", "báo chí", "bao chi"],
    "dong_tien": ["dòng tiền", "dong tien", "khối ngoại", "khoi ngoai", "thanh khoản", "thanh khoan"],
}
TEN_COT_CHI_TIET = {
    "ky_thuat": "Phân tích kỹ thuật",
    "co_ban": "Phân tích cơ bản",
    "tam_ly": "Phân tích tâm lý",
    "dong_tien": "Phân tích dòng tiền",
}
BIEU_TUONG_CHI_TIET = {"ky_thuat": "📈", "co_ban": "🏦", "tam_ly": "📰", "dong_tien": "💵"}

# Cấu trúc file danh_muc.csv - danh mục vị thế TỰ KHAI BÁO bởi người dùng
# (KHÔNG kết nối tài khoản chứng khoán thật, chỉ là "sổ tay" bot nhớ giúp).
COT_DANH_MUC = [
    "ma", "gia_vao", "khoi_luong", "stop_loss", "take_profit",
    "thoi_gian_vao", "trang_thai", "da_canh_bao_sl", "da_canh_bao_tp",
]
# ====================================================

os.makedirs(LOG_FOLDER, exist_ok=True)
# Gắn handler TRỰC TIẾP vào logger "bot_assistant" (thay vì logging.basicConfig cấu
# hình root logger) - vì main.py/data_engine.py cũng tự cấu hình logging khi bị
# import vào đây, và logging.basicConfig() chỉ có tác dụng ở LẦN GỌI ĐẦU TIÊN trong
# cả tiến trình (ai import trước thì "thắng"), khiến log của bot_assistant từng bị
# lạc sang file log của module khác (main.log). Gắn handler trực tiếp + propagate=
# False giúp file này luôn ghi đúng vào bot_assistant.log bất kể thứ tự import.
logger = logging.getLogger("bot_assistant")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    _file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)

# Giảm bớt log rác từ thư viện httpx bên trong python-telegram-bot
logging.getLogger("httpx").setLevel(logging.WARNING)


# ==================== HELPER ====================

def chuan_hoa(text: str) -> str:
    """Ép về cùng 1 dạng Unicode (NFC) trước khi so khớp - tránh lỗi so sánh
    chuỗi tiếng Việt thất bại âm thầm khi input đến từ thiết bị gõ ra dạng
    NFD (VD một số bàn phím/app trên điện thoại)."""
    return unicodedata.normalize("NFC", text)


def loi_chao_dau_ngay() -> str:
    """Câu chào kèm thứ + ngày tháng năm hôm nay."""
    hom_nay = datetime.now()
    ten_thu = TEN_THU_TRONG_TUAN[hom_nay.weekday()]
    return f"📅 Hôm nay là {ten_thu}, ngày {hom_nay.strftime('%d/%m/%Y')}."


async def kiem_tra_va_chao_dau_ngay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nếu đây là tin nhắn ĐẦU TIÊN trong ngày của người này, gửi lời chào
    ngày tháng trước, rồi mới để hàm gọi tiếp tục xử lý câu hỏi như bình thường.
    Mỗi ngày chỉ chào 1 lần/người (dựa vào context.user_data)."""
    hom_nay_str = datetime.now().strftime("%Y-%m-%d")
    ngay_da_luu = context.user_data.get(KHOA_NGAY_GAN_NHAT)
    if ngay_da_luu != hom_nay_str:
        context.user_data[KHOA_NGAY_GAN_NHAT] = hom_nay_str
        await update.message.reply_text(loi_chao_dau_ngay())

# ================================================================


# ==================== TRUY VẤN DỮ LIỆU (READ-ONLY) ====================

def tim_ma_trong_cau(text: str) -> str | None:
    """Dò xem câu hỏi có nhắc tới mã CP nào trong watchlist không (không phân biệt hoa/thường)."""
    text_upper = chuan_hoa(text).upper()
    for ma in WATCHLIST:
        if re.search(rf"\b{ma}\b", text_upper):
            return ma
    return None


def nhan_dien_y_dinh(text: str) -> str:
    """Trả về 'phan_tich' | 'gia' | 'khong_ro' dựa trên từ khóa trong câu hỏi.
    Ưu tiên 'phan_tich' nếu câu hỏi có cả 2 loại từ khóa (VD: "giá FPT có nên mua không")."""
    text_lower = chuan_hoa(text).lower()
    if any(tk in text_lower for tk in TU_KHOA_PHAN_TICH):
        return "phan_tich"
    if any(tk in text_lower for tk in TU_KHOA_GIA):
        return "gia"
    return "khong_ro"


def nhan_dien_loai_chi_tiet(text: str) -> str | None:
    """Trả về 'ky_thuat'|'co_ban'|'tam_ly'|'dong_tien' nếu câu hỏi nhắc rõ 1 mảng phân tích, ngược lại None."""
    text_lower = chuan_hoa(text).lower()
    for loai, tu_khoa in TU_KHOA_CHI_TIET.items():
        if any(tk in text_lower for tk in tu_khoa):
            return loai
    return None


def lay_gia_gan_nhat(ma: str) -> str:
    """Đọc CSV giá, trả về text mô tả phiên gần nhất + % thay đổi so với phiên trước."""
    if not os.path.exists(CSV_GIA_PATH):
        return "⚠️ Chưa có dữ liệu giá trong kho (đang chờ chu kỳ fetch tự động đầu tiên chạy xong)."

    try:
        df = pd.read_csv(CSV_GIA_PATH)
    except Exception as e:
        logger.error(f"Lỗi đọc CSV giá: {e}")
        return "⚠️ Có lỗi khi đọc dữ liệu giá, thử lại sau nhé."

    df_ma = df[df["ma"] == ma].sort_values("time")
    if df_ma.empty:
        return f"⚠️ Chưa có dữ liệu giá cho {ma} trong kho."

    phien_moi_nhat = df_ma.iloc[-1]
    dong_thay_doi = ""
    if len(df_ma) >= 2:
        phien_truoc = df_ma.iloc[-2]
        thay_doi = phien_moi_nhat["close"] - phien_truoc["close"]
        pct = (thay_doi / phien_truoc["close"]) * 100 if phien_truoc["close"] else 0
        bieu_tuong = "🟢" if thay_doi > 0 else ("🔴" if thay_doi < 0 else "⚪")
        dong_thay_doi = f"\n{bieu_tuong} Thay đổi: {thay_doi:+.2f} ({pct:+.2f}%) so với phiên trước"

    return (
        f"📌 *{ma}* - phiên {phien_moi_nhat['time']}\n"
        f"Đóng cửa: *{phien_moi_nhat['close']}*{dong_thay_doi}\n"
        f"Mở: {phien_moi_nhat['open']} | Cao: {phien_moi_nhat['high']} | Thấp: {phien_moi_nhat['low']}\n"
        f"KL: {int(phien_moi_nhat['volume']):,}"
    )


def lay_phan_tich_gan_nhat(ma: str) -> str:
    """Đọc sheet 'Tổng quan' trong file Excel, trả về dòng MỚI NHẤT ứng với mã
    (main.py luôn chèn dòng mới lên đầu -> dòng đầu tiên khớp mã là mới nhất)."""
    if not os.path.exists(XLSX_PATH):
        return "⚠️ Chưa có file phân tích nào được tạo (đang chờ lịch pipeline tự động 11:35/15:15 chạy)."

    try:
        wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
        ws = wb["Tổng quan"]
    except Exception as e:
        logger.error(f"Lỗi đọc Excel phân tích: {e}")
        return "⚠️ Có lỗi khi đọc file phân tích, thử lại sau nhé."

    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    try:
        idx_ma = header.index("Mã")
    except ValueError:
        return "⚠️ Cấu trúc file phân tích không như mong đợi, báo lại cho admin nhé."

    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[idx_ma] == ma:
            d = dict(zip(header, row))
            bieu_tuong = {"BUY": "🟢 MUA", "SELL": "🔴 BÁN", "HOLD": "🟡 GIỮ"}.get(d.get("Trạng thái"), d.get("Trạng thái"))
            dong_gia = ""
            if d.get("Trạng thái") in ("BUY", "SELL"):
                dong_gia = (
                    f"\n💰 Entry: *{d.get('Giá vào')}* | 🛑 SL: *{d.get('Cắt lỗ')}* | 🎯 TP: *{d.get('Chốt lời')}*"
                )
            return (
                f"📊 *{ma}* - {bieu_tuong}\n"
                f"Độ tin cậy: *{d.get('Độ tin cậy')}/100* | Rủi ro: *{d.get('Mức độ rủi ro')}*"
                f"{dong_gia}\n\n"
                f"📝 {d.get('Nhận định & lời khuyên')}\n\n"
                f"🕐 Phân tích lúc: {d.get('Thời gian phân tích')}"
            )

    return f"⚠️ Chưa có kết quả phân tích nào cho {ma} (main.py đã chạy mã này chưa?)."


def lay_phan_tich_chi_tiet(ma: str, loai: str) -> str:
    """Đọc sheet 'Phân tích chi tiết', trả về đoạn giải thích dài ứng với loại
    (ky_thuat/co_ban/tam_ly/dong_tien) của mã, dòng MỚI NHẤT."""
    if not os.path.exists(XLSX_PATH):
        return "⚠️ Chưa có file phân tích nào được tạo (đang chờ lịch pipeline tự động 11:35/15:15 chạy)."

    try:
        wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
        ws = wb["Phân tích chi tiết"]
    except Exception as e:
        logger.error(f"Lỗi đọc sheet Phân tích chi tiết: {e}")
        return "⚠️ Có lỗi khi đọc file phân tích, thử lại sau nhé."

    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    try:
        idx_ma = header.index("Mã")
        idx_cot = header.index(TEN_COT_CHI_TIET[loai])
        idx_thoi_gian = header.index("Thời gian phân tích")
    except ValueError:
        return "⚠️ Cấu trúc sheet Phân tích chi tiết không như mong đợi, báo lại cho admin nhé."

    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[idx_ma] == ma:
            noi_dung = row[idx_cot] or "(chưa có dữ liệu cho phần này)"
            return (
                f"{BIEU_TUONG_CHI_TIET[loai]} *{TEN_COT_CHI_TIET[loai]} - {ma}*\n\n"
                f"{noi_dung}\n\n"
                f"🕐 {row[idx_thoi_gian]}"
            )

    return f"⚠️ Chưa có phân tích chi tiết cho {ma} (main.py đã chạy mã này chưa?)."


def lay_sl_tp_tu_phan_tich(ma: str):
    """Trả về (stop_loss, take_profit) từ lần phân tích gần nhất trong Excel.
    Trả về (None, None) nếu chưa từng phân tích, hoặc tín hiệu gần nhất là
    HOLD (HOLD không có SL/TP vì không phải điểm vào lệnh). Hiện KHÔNG còn
    dùng làm nguồn mặc định cho /muavao nữa (đã đổi sang tính theo %,
    xem tinh_sl_tp_theo_phan_tram) - giữ lại hàm này vì vẫn hữu ích để tham
    khảo SL/TP theo góc nhìn kỹ thuật nếu cần dùng sau này."""
    if not os.path.exists(XLSX_PATH):
        return None, None
    try:
        wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
        ws = wb["Tổng quan"]
    except Exception:
        return None, None

    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    try:
        idx_ma = header.index("Mã")
        idx_sl = header.index("Cắt lỗ")
        idx_tp = header.index("Chốt lời")
    except ValueError:
        return None, None

    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[idx_ma] == ma:
            return row[idx_sl], row[idx_tp]
    return None, None


def lay_trang_thai_va_tp_ky_thuat(ma: str):
    """Trả về (trang_thai, take_profit_ky_thuat) từ lần phân tích gần nhất -
    dùng để tham khảo xu hướng khi đã đạt ngưỡng chốt lời % (xem
    xay_dung_thong_diep_chot_loi). take_profit_ky_thuat là None nếu tín hiệu
    không phải BUY/SELL (HOLD không có TP) hoặc chưa từng phân tích."""
    if not os.path.exists(XLSX_PATH):
        return None, None
    try:
        wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
        ws = wb["Tổng quan"]
    except Exception:
        return None, None

    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    try:
        idx_ma = header.index("Mã")
        idx_trang_thai = header.index("Trạng thái")
        idx_tp = header.index("Chốt lời")
    except ValueError:
        return None, None

    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[idx_ma] == ma:
            return row[idx_trang_thai], row[idx_tp]
    return None, None


def xay_dung_thong_diep_chot_loi(ma: str, gia_hien_tai: float, gia_vao: float, lai_lo_pct: float) -> str:
    """Khi đã đạt ngưỡng chốt lời (%), tham khảo thêm xu hướng kỹ thuật gần
    nhất để đưa lời khuyên có ngữ cảnh - thay vì chỉ báo "chạm TP" khô khan."""
    trang_thai, tp_ky_thuat = lay_trang_thai_va_tp_ky_thuat(ma)

    thong_diep = (
        f"🎯 *{ma}* đã đạt lãi {lai_lo_pct:+.1f}%!\n"
        f"Giá hiện tại: {gia_hien_tai} | Giá vào: {gia_vao}\n\n"
    )

    if trang_thai == "BUY" and tp_ky_thuat and tp_ky_thuat > gia_hien_tai:
        pct_tp_ky_thuat = (tp_ky_thuat - gia_vao) / gia_vao * 100
        thong_diep += (
            f"📈 Phân tích kỹ thuật gần nhất VẪN cho tín hiệu MUA (xu hướng tăng tiếp).\n"
            f"Bạn có thể:\n"
            f"• Chốt lời ngay tại đây với lãi {lai_lo_pct:+.1f}%, hoặc\n"
            f"• Giữ thêm - mục tiêu kỹ thuật kế tiếp khoảng *{tp_ky_thuat}* (~{pct_tp_ky_thuat:+.1f}%), "
            f"nếu bạn chấp nhận rủi ro giá điều chỉnh lại."
        )
    else:
        ten_trang_thai = trang_thai or "chưa có dữ liệu"
        thong_diep += (
            f"📉 Phân tích kỹ thuật gần nhất là *{ten_trang_thai}* - không còn tín hiệu tăng tiếp rõ ràng.\n"
            f"Nên cân nhắc chốt lời ngay tại mức này."
        )

    return thong_diep           

def tinh_sl_tp_theo_phan_tram(gia_vao: float):
    """Tự tính SL/TP theo % CỐ ĐỊNH trên giá vào (NGUONG_CAT_LO_PCT /
    NGUONG_CHOT_LOI_PCT) - không phụ thuộc phân tích kỹ thuật nên LUÔN tính
    được cho mọi mã, mọi thời điểm. Đây là nguồn SL/TP MẶC ĐỊNH của /muavao."""
    sl = round(gia_vao * (1 - NGUONG_CAT_LO_PCT / 100), 2)
    tp = round(gia_vao * (1 + NGUONG_CHOT_LOI_PCT / 100), 2)
    return sl, tp


def lay_thoi_gian_phan_tich_gan_nhat() -> datetime | None:
    """Đọc thời điểm phân tích của lần chạy pipeline GẦN NHẤT (dòng đầu tiên
    trong sheet 'Tổng quan', vì main.py luôn chèn dòng mới lên đầu). Dùng để
    biết hôm nay đã có phân tích nào chưa - phục vụ cơ chế chạy bù."""
    if not os.path.exists(XLSX_PATH):
        return None
    try:
        wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
        ws = wb["Tổng quan"]
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        idx_tg = header.index("Thời gian phân tích")
        for row in ws.iter_rows(min_row=2, max_row=2, values_only=True):
            gia_tri = row[idx_tg]
            if gia_tri:
                return datetime.strptime(str(gia_tri), "%Y-%m-%d %H:%M:%S")
    except Exception as e:
        logger.error(f"Lỗi đọc thời gian phân tích gần nhất: {e}")
    return None

# ================================================================


# ==================== DANH MỤC CÁ NHÂN (đọc/ghi CSV) ====================
# QUAN TRỌNG: đây là danh mục do NGƯỜI DÙNG TỰ KHAI BÁO qua /muavao, KHÔNG
# kết nối bất kỳ tài khoản chứng khoán thật nào. Bot chỉ đóng vai trò "sổ
# tay biết tính toán" - nhớ giúp vị thế + tự canh giá, quyền quyết định mua/
# bán thật vẫn hoàn toàn ở người dùng.

def doc_danh_muc() -> pd.DataFrame:
    """Đọc file danh_muc.csv, tạo DataFrame rỗng đúng cấu trúc nếu chưa tồn tại."""
    if not os.path.exists(DANH_MUC_PATH):
        return pd.DataFrame(columns=COT_DANH_MUC)
    try:
        df = pd.read_csv(DANH_MUC_PATH)
        for c in ("da_canh_bao_sl", "da_canh_bao_tp"):
            if c in df.columns:
                df[c] = df[c].astype(bool)
        return df
    except Exception as e:
        logger.error(f"Lỗi đọc file danh mục: {e}")
        return pd.DataFrame(columns=COT_DANH_MUC)


def ghi_danh_muc(df: pd.DataFrame, max_retries: int = 5) -> bool:
    """Ghi đè toàn bộ danh mục - atomic write (.tmp + os.replace) để không
    bao giờ để lại file nửa vời nếu bị ngắt giữa chừng, tự retry nếu bị khóa file."""
    tmp_path = DANH_MUC_PATH + ".tmp"
    for attempt in range(max_retries):
        try:
            df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
            os.replace(tmp_path, DANH_MUC_PATH)
            return True
        except PermissionError:
            logger.warning(f"File danh mục đang bị khóa - thử lại (lần {attempt + 1}/{max_retries})...")
            time.sleep(3)
        except Exception as e:
            logger.error(f"Lỗi ghi file danh mục (lần {attempt + 1}/{max_retries}): {e}")
            time.sleep(3)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    logger.error("Không ghi được file danh mục sau nhiều lần thử.")
    return False

# ================================================================


# ==================== TELEGRAM HANDLERS ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Chào bạn! Tớ là trợ lý theo dõi cổ phiếu của vn_stock_agent.\n\n"
        "Hỏi tớ tự nhiên kiểu:\n"
        "• \"giá VCB hôm nay\"\n"
        "• \"FPT có nên mua không\"\n"
        "• \"kỹ thuật ACB\", \"dòng tiền FPT\"\n\n"
        "Hoặc dùng lệnh /help để xem đầy đủ."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Danh sách lệnh*\n"
        "/gia <MÃ> - giá phiên gần nhất\n"
        "/phantich <MÃ> - tín hiệu & khuyến nghị gần nhất\n"
        "/watchlist - danh sách mã đang theo dõi\n\n"
        "📁 *Danh mục cá nhân (tự khai báo)*\n"
        "/muavao <MÃ> <GIÁ> <KL> [SL] [TP] - khai báo vị thế mới\n"
        f"  VD: /muavao VCB 22.5 500 (SL/TP tự tính: lỗ {NGUONG_CAT_LO_PCT}% cắt lỗ, lãi {NGUONG_CHOT_LOI_PCT}% chốt lời)\n"
        "  Muốn tự đặt SL/TP riêng: /muavao VCB 22.5 500 21.5 24.0\n"
        "/danhmuc - xem toàn bộ vị thế đang giữ + lãi/lỗ tạm tính\n"
        "/dachot <MÃ> - đóng vị thế đã chốt xong\n\n"
        "Hoặc chat tự nhiên, VD: \"giá HPG bao nhiêu\", \"MBB nên bán không\",\n"
        "\"kỹ thuật ACB\", \"dòng tiền FPT\" 💬\n\n"
        "⏱️ Giá tự cập nhật mỗi 30 phút (cũng tự canh SL/TP danh mục luôn).\n"
        "Phân tích đầy đủ tự chạy lúc 11:35 & 15:15 hàng ngày (tự chạy bù nếu bot khởi động muộn, lỡ hết các mốc trong ngày).",
        parse_mode="Markdown",
    )


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await kiem_tra_va_chao_dau_ngay(update, context)
    await update.message.reply_text("📋 Danh sách đang theo dõi:\n" + ", ".join(WATCHLIST))


async def cmd_gia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await kiem_tra_va_chao_dau_ngay(update, context)
    if not context.args:
        await update.message.reply_text("Dùng: /gia VCB")
        return
    ma = context.args[0].upper()
    if ma not in WATCHLIST:
        await update.message.reply_text(f"⚠️ {ma} không nằm trong watchlist đang theo dõi.")
        return
    context.user_data[KHOA_MA_GAN_NHAT] = ma
    await update.message.reply_text(lay_gia_gan_nhat(ma), parse_mode="Markdown")


async def cmd_phantich(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await kiem_tra_va_chao_dau_ngay(update, context)
    if not context.args:
        await update.message.reply_text("Dùng: /phantich VCB")
        return
    ma = context.args[0].upper()
    if ma not in WATCHLIST:
        await update.message.reply_text(f"⚠️ {ma} không nằm trong watchlist đang theo dõi.")
        return
    context.user_data[KHOA_MA_GAN_NHAT] = ma
    await update.message.reply_text(lay_phan_tich_gan_nhat(ma), parse_mode="Markdown")


async def cmd_muavao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/muavao MÃ GIÁ KHỐI_LƯỢNG [SL] [TP] - khai báo vị thế mới vào danh mục.
    SL/TP: nếu bạn nhập tay cả 2 thì dùng luôn giá trị đó; nếu để trống, TỰ
    TÍNH theo % cố định trên giá vào (NGUONG_CAT_LO_PCT/NGUONG_CHOT_LOI_PCT)
    - luôn tính được, không phụ thuộc mã đó đã từng phân tích hay chưa."""
    await kiem_tra_va_chao_dau_ngay(update, context)
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Dùng: /muavao MÃ GIÁ KHỐI_LƯỢNG [SL] [TP]\n"
            "VD: /muavao VCB 22.5 500\n"
            f"(SL/TP để trống thì tớ tự tính: lỗ {NGUONG_CAT_LO_PCT}% cắt lỗ, lãi {NGUONG_CHOT_LOI_PCT}% chốt lời)"
        )
        return

    ma = args[0].upper()
    if ma not in WATCHLIST:
        await update.message.reply_text(f"⚠️ {ma} không nằm trong watchlist đang theo dõi.")
        return

    try:
        gia_vao = float(args[1])
        khoi_luong = float(args[2])
    except ValueError:
        await update.message.reply_text("⚠️ Giá và khối lượng phải là số. VD: /muavao VCB 22.5 500")
        return

    sl_nhap = None
    tp_nhap = None
    try:
        if len(args) >= 4:
            sl_nhap = float(args[3])
        if len(args) >= 5:
            tp_nhap = float(args[4])
    except ValueError:
        await update.message.reply_text("⚠️ SL/TP phải là số. VD: /muavao VCB 22.5 500 21.5 24.0")
        return

    # SL/TP tự tính theo % cố định trên giá vào - LUÔN tính được, không còn
    # trường hợp "chưa có SL/TP" như khi lấy từ phân tích kỹ thuật nữa.
    sl_tu_dong, tp_tu_dong = tinh_sl_tp_theo_phan_tram(gia_vao)
    sl = sl_nhap if sl_nhap is not None else sl_tu_dong
    tp = tp_nhap if tp_nhap is not None else tp_tu_dong

    df = doc_danh_muc()
    dong_moi = {
        "ma": ma, "gia_vao": gia_vao, "khoi_luong": khoi_luong,
        "stop_loss": sl, "take_profit": tp,
        "thoi_gian_vao": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trang_thai": "dang_giu", "da_canh_bao_sl": False, "da_canh_bao_tp": False,
    }
    df = pd.concat([df, pd.DataFrame([dong_moi])], ignore_index=True)

    if not ghi_danh_muc(df):
        await update.message.reply_text("⚠️ Có lỗi khi lưu vị thế, thử lại sau nhé.")
        return

    nguon_sltp = "tự tính theo %" if (sl_nhap is None or tp_nhap is None) else "bạn tự đặt"
    await update.message.reply_text(
        f"✅ Đã ghi nhận vị thế *{ma}*\n"
        f"💰 Giá vào: {gia_vao} | KL: {int(khoi_luong):,}\n"
        f"🛑 SL: {sl} (-{NGUONG_CAT_LO_PCT}%) | 🎯 TP: {tp} (+{NGUONG_CHOT_LOI_PCT}%)\n"
        f"({nguon_sltp})\n\n"
        f"Tớ sẽ tự canh giá mỗi 30 phút và cảnh báo khi chạm SL/TP nhé!",
        parse_mode="Markdown",
    )


async def cmd_danhmuc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/danhmuc - liệt kê toàn bộ vị thế đang giữ, kèm giá hiện tại + lãi/lỗ tạm tính."""
    await kiem_tra_va_chao_dau_ngay(update, context)
    df = doc_danh_muc()
    df_dang_giu = df[df["trang_thai"] == "dang_giu"] if not df.empty else df

    if df_dang_giu.empty:
        await update.message.reply_text("📁 Bạn chưa khai báo vị thế nào đang giữ. Dùng /muavao để thêm nhé.")
        return

    df_gia = pd.read_csv(CSV_GIA_PATH) if os.path.exists(CSV_GIA_PATH) else pd.DataFrame()

    cac_dong = ["📁 *Danh mục đang giữ:*"]
    for _, r in df_dang_giu.iterrows():
        ma = r["ma"]
        dong = (
            f"\n*{ma}*\n"
            f"Vào: {r['gia_vao']} | KL: {int(r['khoi_luong']):,}\n"
            f"🛑 SL: {r['stop_loss']} | 🎯 TP: {r['take_profit']}"
        )

        if not df_gia.empty:
            df_ma = df_gia[df_gia["ma"] == ma].sort_values("time")
            if not df_ma.empty:
                gia_hien_tai = df_ma.iloc[-1]["close"]
                lai_lo_pct = (gia_hien_tai - r["gia_vao"]) / r["gia_vao"] * 100 if r["gia_vao"] else 0
                bieu_tuong = "🟢" if lai_lo_pct >= 0 else "🔴"
                dong += f"\nGiá hiện tại: *{gia_hien_tai}* {bieu_tuong} ({lai_lo_pct:+.2f}%)"

        cac_dong.append(dong)

    await update.message.reply_text("\n".join(cac_dong), parse_mode="Markdown")


async def cmd_dachot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dachot MÃ - đóng vị thế (chuyển trạng thái sang 'da_chot', vẫn giữ lại làm lịch sử)."""
    await kiem_tra_va_chao_dau_ngay(update, context)
    if not context.args:
        await update.message.reply_text("Dùng: /dachot VCB")
        return

    ma = context.args[0].upper()
    df = doc_danh_muc()
    if df.empty:
        await update.message.reply_text(f"⚠️ Bạn chưa khai báo vị thế nào cả.")
        return

    mask = (df["ma"] == ma) & (df["trang_thai"] == "dang_giu")
    if not mask.any():
        await update.message.reply_text(f"⚠️ Không tìm thấy vị thế đang giữ nào cho {ma}.")
        return

    df.loc[mask, "trang_thai"] = "da_chot"
    if not ghi_danh_muc(df):
        await update.message.reply_text("⚠️ Có lỗi khi cập nhật, thử lại sau nhé.")
        return

    await update.message.reply_text(f"✅ Đã đóng vị thế {ma}, chuyển vào lịch sử.")


async def xu_ly_tin_nhan_tu_nhien(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bắt tin nhắn text thường (không phải lệnh /...), cố gắng hiểu ý định + mã CP.
    Nếu câu không có mã, dùng lại mã vừa hỏi gần nhất (nếu có) thay vì báo lỗi ngay."""
    await kiem_tra_va_chao_dau_ngay(update, context)
    text = update.message.text or ""
    ma = tim_ma_trong_cau(text)

    if not ma:
        ma_gan_nhat = context.user_data.get(KHOA_MA_GAN_NHAT)
        if ma_gan_nhat:
            ma = ma_gan_nhat
        else:
            await update.message.reply_text(
                "🤔 Tớ chưa nhận ra bạn đang hỏi về mã nào trong watchlist.\n"
                "Thử lại kiểu \"giá VCB\" hoặc gõ /watchlist để xem danh sách mã nhé."
            )
            return
    else:
        # Chỉ khi câu hỏi CÓ nhắc mã mới cập nhật "mã gần nhất" - tránh việc
        # 1 câu không mã vô tình đổi context sang mã cũ đã lưu trước đó.
        context.user_data[KHOA_MA_GAN_NHAT] = ma

    loai_chi_tiet = nhan_dien_loai_chi_tiet(text)
    if loai_chi_tiet:
        await update.message.reply_text(lay_phan_tich_chi_tiet(ma, loai_chi_tiet), parse_mode="Markdown")
        return

    y_dinh = nhan_dien_y_dinh(text)
    if y_dinh == "phan_tich":
        await update.message.reply_text(lay_phan_tich_gan_nhat(ma), parse_mode="Markdown")
    elif y_dinh == "gia":
        await update.message.reply_text(lay_gia_gan_nhat(ma), parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"Tớ không chắc bạn muốn xem giá hay phân tích {ma}, gửi cả 2 nhé 👇",
        )
        await update.message.reply_text(lay_gia_gan_nhat(ma), parse_mode="Markdown")
        await update.message.reply_text(lay_phan_tich_gan_nhat(ma), parse_mode="Markdown")


async def xu_ly_loi(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Lỗi khi xử lý update: {context.error}")

# ================================================================


# ==================== JOB CHẠY NỀN (SCHEDULER) ====================
# Chạy trong thread riêng của BackgroundScheduler - KHÔNG chặn vòng lặp
# nghe Telegram ở thread chính.

_market = None       # khởi tạo 1 lần khi start, dùng lại cho mọi chu kỳ fetch
_da_luu_gia = None    # set (mã, ngày) đã lưu - nạp 1 lần, cập nhật dần trong bộ nhớ


def job_kiem_tra_danh_muc():
    """So giá mới nhất với SL/TP của từng vị thế ĐANG GIỮ trong danh mục cá
    nhân - chạm ngưỡng nào thì push cảnh báo Telegram + đánh dấu đã cảnh báo
    (để không lặp lại cảnh báo mỗi 30 phút cho cùng 1 lần chạm giá)."""
    try:
        df = doc_danh_muc()
        df_dang_giu = df[df["trang_thai"] == "dang_giu"] if not df.empty else df
        if df_dang_giu.empty or not os.path.exists(CSV_GIA_PATH):
            return

        df_gia = pd.read_csv(CSV_GIA_PATH)
        thay_doi = False

        for idx, r in df_dang_giu.iterrows():
            ma = r["ma"]
            df_ma = df_gia[df_gia["ma"] == ma].sort_values("time")
            if df_ma.empty:
                continue
            gia_hien_tai = df_ma.iloc[-1]["close"]

            if gia_hien_tai <= r["stop_loss"] and not bool(r["da_canh_bao_sl"]):
                bot_telegram.gui_tin_nhan(
                    f"🛑 *{ma}* đã CHẠM STOP LOSS!\n"
                    f"Giá hiện tại: {gia_hien_tai} (SL: {r['stop_loss']})\n"
                    f"Giá vào: {r['gia_vao']} | Cân nhắc cắt lỗ."
                )
                df.loc[idx, "da_canh_bao_sl"] = True
                thay_doi = True

            if gia_hien_tai >= r["take_profit"] and not bool(r["da_canh_bao_tp"]):
                lai_lo_pct = (gia_hien_tai - r["gia_vao"]) / r["gia_vao"] * 100 if r["gia_vao"] else 0
                bot_telegram.gui_tin_nhan(
                    xay_dung_thong_diep_chot_loi(ma, gia_hien_tai, r["gia_vao"], lai_lo_pct)
                )
                df.loc[idx, "da_canh_bao_tp"] = True
                thay_doi = True

        if thay_doi:
            ghi_danh_muc(df)
            logger.info("⏱️ [Scheduler] Đã cập nhật cảnh báo SL/TP cho danh mục.")

    except Exception as e:
        logger.error(f"⏱️ [Scheduler] Lỗi khi kiểm tra danh mục: {e}")


def job_fetch_gia():
    """Fetch giá 1 chu kỳ - y hệt logic data_engine.py, chỉ khác là được
    BackgroundScheduler gọi lại mỗi N phút thay vì tự while True + sleep.
    Sau khi fetch xong, LUÔN kiểm tra luôn danh mục cá nhân với giá mới nhất."""
    global _market, _da_luu_gia
    try:
        tong_dong_moi = data_engine.chay_mot_chu_ky(_market, _da_luu_gia)
        logger.info(f"⏱️ [Scheduler] Fetch giá xong: {tong_dong_moi} phiên mới.")
    except Exception as e:
        logger.error(f"⏱️ [Scheduler] Lỗi khi fetch giá tự động: {e}")
    finally:
        # Kiểm tra danh mục dù fetch giá có lỗi hay không - vẫn nên thử so
        # với dữ liệu giá cũ nhất hiện có, còn hơn bỏ qua hẳn 1 chu kỳ.
        job_kiem_tra_danh_muc()


def job_chay_pipeline():
    """Chạy toàn bộ pipeline phân tích (TA -> Risk -> FA -> Sentiment -> Brain)
    + lưu Excel + push cảnh báo Telegram - y hệt main.py, chỉ khác là được gọi
    theo lịch cố định (hoặc chạy bù) thay vì chạy tay."""
    try:
        logger.info("⏱️ [Scheduler] Bắt đầu chạy pipeline phân tích tự động...")
        pipeline.chay_toan_bo_watchlist()
        logger.info("⏱️ [Scheduler] Hoàn tất pipeline phân tích tự động.")
    except Exception as e:
        logger.error(f"⏱️ [Scheduler] Lỗi khi chạy pipeline tự động: {e}")


def kiem_tra_va_bu_pipeline_neu_can(scheduler: BackgroundScheduler):
    """CƠ CHẾ CHẠY BÙ: nếu bot khởi động muộn và đã lỡ ÍT NHẤT 1 mốc giờ
    pipeline hôm nay, MÀ hôm nay CHƯA có lần phân tích nào cả - tự động lên
    lịch chạy bù 1 lần (không phải chạy bù cho TỪNG mốc đã lỡ, vì mỗi lần
    chạy tốn quota Gemini đáng kể - chỉ cần đảm bảo có ít nhất 1 lần/ngày).
    Job chạy bù được đặt trễ 15s sau khi gọi hàm này, để không chặn/xung đột
    với job_fetch_gia cũng đang chạy ngay lúc khởi động."""
    now = datetime.now()
    cac_moc_da_qua = [(g, p) for g, p in GIO_CHAY_PIPELINE if (g, p) <= (now.hour, now.minute)]
    if not cac_moc_da_qua:
        logger.info("⏱️ Chưa qua mốc pipeline nào hôm nay, không cần chạy bù.")
        return

    lan_gan_nhat = lay_thoi_gian_phan_tich_gan_nhat()
    if lan_gan_nhat is not None and lan_gan_nhat.date() == now.date():
        logger.info(f"✅ Hôm nay đã có phân tích lúc {lan_gan_nhat.strftime('%H:%M')} rồi, không cần chạy bù.")
        return

    logger.warning(
        f"⚠️ Đã lỡ {len(cac_moc_da_qua)} mốc pipeline hôm nay ({', '.join(f'{g:02d}:{p:02d}' for g, p in cac_moc_da_qua)}) "
        f"và hôm nay chưa có phân tích nào - lên lịch chạy bù 1 lần sau 15s..."
    )
    scheduler.add_job(
        job_chay_pipeline, "date",
        run_date=datetime.now() + timedelta(seconds=15),
        id="pipeline_chay_bu",
    )


def khoi_tao_scheduler() -> BackgroundScheduler:
    """Khởi tạo Market() + nạp lịch sử đã lưu 1 lần, rồi đăng ký các job nền:
    - fetch giá + kiểm tra danh mục mỗi KHOANG_CACH_CHU_KY_PHUT phút (chạy
      ngay 1 lần lúc start)
    - chạy pipeline phân tích đúng các mốc giờ trong GIO_CHAY_PIPELINE
    - kiểm tra và chạy bù pipeline nếu bot khởi động muộn, lỡ hết mốc trong ngày"""
    global _market, _da_luu_gia

    logger.info("📚 Đang khởi tạo Data Engine (nạp lịch sử giá đã lưu)...")
    _market = Market()
    _da_luu_gia = data_engine.storage_doc_lich_su_da_luu()
    logger.info(f"📚 Đã nạp {len(_da_luu_gia)} phiên lịch sử có sẵn.")

    scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")

    scheduler.add_job(
        job_fetch_gia, "interval",
        minutes=data_engine.KHOANG_CACH_CHU_KY_PHUT,
        id="fetch_gia",
        next_run_time=datetime.now(),  # chạy ngay 1 lần lúc khởi động, không đợi đủ 30p
    )

    for gio, phut in GIO_CHAY_PIPELINE:
        scheduler.add_job(
            job_chay_pipeline, "cron",
            hour=gio, minute=phut,
            id=f"pipeline_{gio:02d}{phut:02d}",
        )

    scheduler.start()
    gio_hien_thi = ", ".join(f"{g:02d}:{p:02d}" for g, p in GIO_CHAY_PIPELINE)
    logger.info(
        f"⏱️ Đã bật lịch tự động: fetch giá + canh danh mục mỗi "
        f"{data_engine.KHOANG_CACH_CHU_KY_PHUT} phút, phân tích lúc {gio_hien_thi}."
    )

    kiem_tra_va_bu_pipeline_neu_can(scheduler)

    return scheduler

# ================================================================


def main():
    if not BOT_TOKEN:
        logger.error("Chưa có TELEGRAM_BOT_TOKEN trong .env - không khởi động được bot.")
        return

    khoi_tao_scheduler()  # chỉ chạy 1 lần duy nhất - không đặt trong vòng lặp bên dưới,
                           # tránh đăng ký trùng job mỗi lần bot tự khởi động lại

    so_lan_thu = 0
    while True:
        try:
            app = Application.builder().token(BOT_TOKEN).build()
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("help", cmd_help))
            app.add_handler(CommandHandler("watchlist", cmd_watchlist))
            app.add_handler(CommandHandler("gia", cmd_gia))
            app.add_handler(CommandHandler("phantich", cmd_phantich))
            app.add_handler(CommandHandler("muavao", cmd_muavao))
            app.add_handler(CommandHandler("danhmuc", cmd_danhmuc))
            app.add_handler(CommandHandler("dachot", cmd_dachot))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, xu_ly_tin_nhan_tu_nhien))
            app.add_error_handler(xu_ly_loi)

            logger.info("🤖 Bot đã khởi động ĐẦY ĐỦ (nghe chat + fetch giá + pipeline + danh mục tự động)...")
            so_lan_thu = 0  # reset về 0 mỗi khi chạy ổn định lại
            app.run_polling(allowed_updates=Update.ALL_TYPES)
            break  # chỉ tới đây khi dừng chủ động (Ctrl+C) - thoát hẳn, không retry

        except KeyboardInterrupt:
            logger.info("🛑 Đã dừng bot theo yêu cầu (Ctrl+C).")
            break

        except Exception as e:
            so_lan_thu += 1
            cho_giay = min(60, 5 * so_lan_thu)  # chờ tăng dần: 5s, 10s, 15s... tối đa 60s
            logger.error(f"⚠️ Bot bị rớt (thường do mạng chập chờn) - lần {so_lan_thu}: {e}")
            logger.info(f"🔄 Tự khởi động lại sau {cho_giay}s...")
            time.sleep(cho_giay)


if __name__ == "__main__":
    main()
