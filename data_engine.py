"""
MODULE 1: DATA ENGINE & CACHING
================================
Chịu trách nhiệm lấy dữ liệu OHLCV từ vnstock và lưu trữ.

Kiến trúc file này được tách làm 3 lớp rõ ràng:
  1. STORAGE LAYER  - đọc/ghi dữ liệu (hiện tại: CSV). Khi nào chuyển sang
                       SQLite, CHỈ cần sửa các hàm trong lớp này, phần
                       FETCH LAYER và ORCHESTRATION không cần đổi gì.
  2. FETCH LAYER     - gọi vnstock để lấy dữ liệu, có retry khi dính rate limit.
  3. ORCHESTRATION   - vòng lặp chính điều phối 2 lớp trên.

Kế thừa từ agent_data.py (Sprint 1), refactor thêm logging + tách lớp.
"""

from datetime import datetime, timedelta
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import time
import logging
import pandas as pd
from vnstock.ui import Market


# ==================== CẤU HÌNH ====================
NGAY_LAY_DU_LIEU = 7                 # chỉ lấy 7 ngày gần nhất mỗi lần fetch
KHOANG_CACH_CHU_KY_PHUT = 30         # 30 phút/lần, chạy 24/7
CSV_FOLDER = r"C:\vn_stock_agent_data"     # nằm ngoài OneDrive để tránh bị khóa file khi đang đồng bộ
os.makedirs(CSV_FOLDER, exist_ok=True)
CSV_PATH = os.path.join(CSV_FOLDER, "lich_su_gia.csv")

LOG_FOLDER = os.path.join(CSV_FOLDER, "logs")
os.makedirs(LOG_FOLDER, exist_ok=True)
LOG_PATH = os.path.join(LOG_FOLDER, "data_engine.log")

watchlist_enterprise = [
    "VCB", "BID", "TCB", "VPB", "MBB", "ACB",
    "HDB", "VRE", "OCB", "LPB", "HPG", "FPT",
]
WATCHLIST = watchlist_enterprise
# ====================================================


# ==================== LOGGING SETUP ====================
logger = logging.getLogger("data_engine")
logger.setLevel(logging.INFO)

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(_formatter)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)

if not logger.handlers:
    logger.addHandler(_file_handler)
    logger.addHandler(_console_handler)
# =========================================================


# ==================== STORAGE LAYER (CSV) ====================
# Đây là lớp duy nhất "biết" dữ liệu đang nằm trong CSV.
# Khi chuyển sang SQLite: chỉ sửa 3 hàm dưới đây, giữ nguyên chữ ký hàm
# (tên hàm + tham số + kiểu dữ liệu trả về) để không phải sửa chỗ khác.

def storage_doc_lich_su_da_luu() -> set:
    """Đọc dữ liệu đã lưu để biết dòng nào đã có rồi, tránh ghi trùng mỗi chu kỳ.
    Trả về: set các tuple (ma, time) đã tồn tại."""
    da_luu = set()
    if os.path.exists(CSV_PATH):
        try:
            df_cu = pd.read_csv(CSV_PATH, usecols=["ma", "time"])
            da_luu = set(zip(df_cu["ma"].astype(str), df_cu["time"].astype(str)))
        except Exception as e:
            logger.warning(f"⚠️ Không đọc được lịch sử đã lưu, sẽ tạo lại: {e}")
    return da_luu


def storage_ghi_them(df_moi: pd.DataFrame, max_retries: int = 5, wait_seconds: int = 3) -> bool:
    """Ghi thêm dữ liệu mới vào storage, tự retry nếu bị khóa file (OneDrive sync).
    Trả về True/False cho biết ghi thành công hay không."""
    file_exists = os.path.exists(CSV_PATH)
    for attempt in range(max_retries + 1):
        try:
            df_moi.to_csv(CSV_PATH, mode="a", header=not file_exists, index=False, encoding="utf-8-sig")
            return True
        except PermissionError as e:
            if attempt < max_retries:
                logger.warning(
                    f"⚠️ File dữ liệu đang bị khóa (có thể do OneDrive đang sync) "
                    f"- chờ {wait_seconds}s rồi thử lại (lần {attempt + 1}/{max_retries})..."
                )
                time.sleep(wait_seconds)
                continue
            logger.error(f"❌ Vẫn không ghi được dữ liệu sau {max_retries} lần thử: {e}")
            return False
    return False


def storage_kiem_tra_ton_tai() -> bool:
    """Kiểm tra storage đã có dữ liệu chưa (dùng để quyết định có ghi header hay không)."""
    return os.path.exists(CSV_PATH)

# ================================================================


# ==================== FETCH LAYER (vnstock) ====================

def retry_on_rate_limit(func, max_retries=5, wait_seconds=60, label=""):
    """
    Gọi func(). Nếu dính rate limit (kể cả khi vnstock thoát bằng SystemExit)
    thì chờ wait_seconds giây rồi thử lại, tối đa max_retries lần.
    Nếu lỗi không liên quan rate limit thì raise ngay để except bên ngoài xử lý.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except (Exception, SystemExit) as e:
            msg = str(e).lower()
            is_rate_limit = (
                isinstance(e, SystemExit)
                or "giới hạn" in msg
                or "rate limit" in msg
                or "429" in msg
            )
            if is_rate_limit and attempt < max_retries:
                logger.warning(
                    f"⚠️ [{label}] Dính rate limit - chờ {wait_seconds}s rồi thử lại "
                    f"(lần {attempt + 1}/{max_retries})..."
                )
                time.sleep(wait_seconds)
                continue
            raise

# ================================================================


# ==================== XỬ LÝ DỮ LIỆU TRUNG GIAN ====================

def loc_va_chuan_bi_du_lieu_moi(df: pd.DataFrame, ma: str, da_luu: set) -> pd.DataFrame:
    """Gắn mã CP + loại bỏ những dòng đã có trong storage, trả về DataFrame chỉ gồm dòng mới."""
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["ma"] = ma
    # QUAN TRONG - DA TUNG BI BUG NAY 2 LAN: KHONG dung .astype(str) truc tiep.
    # vnstock/pandas co the tra ve timestamp voi format khac nhau tuy nguon fetch
    # (VD '2026-08-14 07:00:00' vs '8/14/2026 07:00' neu file CSV lo bi Excel mo/luu
    # tu dong doi dinh dang). Neu khong chuan hoa, CUNG 1 phien giao dich se bi luu
    # thanh 2 dong khac nhau, va khi sap xep de tim "phien moi nhat" (main.py,
    # bot_telegram.py deu dua vao) se SAI HOAN TOAN do so sanh string, khong phai
    # so sanh ngay thang that. Parse ve datetime that (format='mixed' de doc duoc
    # ca 2 kieu), roi chuan hoa VE DUY NHAT 1 dang YYYY-MM-DD (bo gio vi day la
    # du lieu theo phien/ngay, gio luon co dinh 07:00:00, khong co y nghia gi).
    df["time"] = pd.to_datetime(df["time"], format="mixed", dayfirst=False).dt.strftime("%Y-%m-%d")
    # Loại trùng NGAY TRONG cùng 1 lần fetch (API đôi khi trả 2 dòng giống hệt
    # nhau cho phiên hiện tại khi thị trường đang mở) - giữ dòng cuối vì đó
    # thường là bản mới nhất/đầy đủ nhất.
    df = df.drop_duplicates(subset=["time"], keep="last")
    df["thoi_diem_ghi_nhan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    moi_mask = ~df.apply(lambda r: (r["ma"], r["time"]) in da_luu, axis=1)
    return df[moi_mask]


def luu_vao_storage(df: pd.DataFrame, ma: str, da_luu: set) -> int:
    """Lọc dòng mới rồi ghi vào storage. Trả về số dòng mới đã ghi thành công."""
    df_moi = loc_va_chuan_bi_du_lieu_moi(df, ma, da_luu)
    if df_moi.empty:
        return 0

    thanh_cong = storage_ghi_them(df_moi)
    if not thanh_cong:
        return 0

    for _, r in df_moi.iterrows():
        da_luu.add((r["ma"], r["time"]))

    return len(df_moi)

# ================================================================


# ==================== ORCHESTRATION ====================

def chay_mot_chu_ky(mkt, da_luu):
    """Fetch VN-Index + toàn bộ watchlist 1 lượt, chỉ lưu vào storage những phiên mới."""
    ngay_bat_dau = (datetime.now() - timedelta(days=NGAY_LAY_DU_LIEU)).strftime("%Y-%m-%d")
    ngay_ket_thuc = datetime.now().strftime("%Y-%m-%d")
    tong_dong_moi = 0

    # 1. VN-INDEX
    try:
        logger.info(f"📥 Đang tải VN-Index ({NGAY_LAY_DU_LIEU} ngày gần nhất)...")
        vnindex_df = retry_on_rate_limit(
            lambda: mkt.index("VNINDEX").ohlcv(start=ngay_bat_dau, end=ngay_ket_thuc),
            label="VNINDEX"
        )
        so_dong = luu_vao_storage(vnindex_df, "VNINDEX", da_luu)
        tong_dong_moi += so_dong
        logger.info(f"✅ VN-Index: {so_dong} phiên mới được lưu")
    except Exception as e:
        logger.error(f"❌ Lỗi khi tải VN-Index: {e}")

    # 2. TỪNG MÃ TRONG WATCHLIST
    for ma_cp in watchlist_enterprise:
        try:
            logger.info(f"📥 Đang tải {ma_cp}...")
            df = retry_on_rate_limit(
                lambda: mkt.equity(ma_cp).ohlcv(start=ngay_bat_dau, end=ngay_ket_thuc),
                label=ma_cp
            )
            so_dong = luu_vao_storage(df, ma_cp, da_luu)
            tong_dong_moi += so_dong
            logger.info(f"✅ {ma_cp}: {so_dong} phiên mới được lưu")
        except Exception as e:
            logger.error(f"❌ Lỗi khi tải dữ liệu cổ phiếu {ma_cp}: {e}")
        finally:
            time.sleep(4)

    return tong_dong_moi


def main():
    logger.info(f"🚀 Bắt đầu Data Engine (mỗi {KHOANG_CACH_CHU_KY_PHUT} phút/chu kỳ)")
    logger.info(f"📁 Dữ liệu sẽ được lưu vào: {CSV_PATH}")
    logger.info(f"📝 Log chi tiết tại: {LOG_PATH}")
    logger.info("🛑 Nhấn Ctrl+C để dừng.")

    mkt = Market()
    da_luu = storage_doc_lich_su_da_luu()
    logger.info(f"📚 Đã nạp {len(da_luu)} phiên lịch sử có sẵn.")

    so_chu_ky = 0
    while True:
        so_chu_ky += 1
        bat_dau = time.time()
        logger.info(f"{'='*60}")
        logger.info(f"🔄 CHU KỲ #{so_chu_ky} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"{'='*60}")

        try:
            tong_dong_moi = chay_mot_chu_ky(mkt, da_luu)
            logger.info(f"📊 Tổng kết chu kỳ #{so_chu_ky}: {tong_dong_moi} phiên mới được ghi")
        except Exception as e:
            logger.error(f"❌ Lỗi không xác định trong chu kỳ #{so_chu_ky}: {e}")

        thoi_gian_chay = time.time() - bat_dau
        thoi_gian_nghi = max(0, KHOANG_CACH_CHU_KY_PHUT * 60 - thoi_gian_chay)
        logger.info(f"😴 Chu kỳ mất {thoi_gian_chay:.0f}s. Nghỉ {thoi_gian_nghi/60:.1f} phút...")
        time.sleep(thoi_gian_nghi)

# ================================================================


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Đã dừng Data Engine (Ctrl+C).")

# ==================== ALIAS TƯƠNG THÍCH VỚI backfill_lich_su.py ====================
# backfill_lich_su.py dùng tên hàm phiên bản cũ, còn file này đã đổi tên khi refactor.
# Alias để backfill_lich_su.py chạy được mà không phải sửa nó.
WATCHLIST = watchlist_enterprise
storage_nap_du_lieu_da_luu = storage_doc_lich_su_da_luu
chuan_hoa_va_loc_dong_moi = loc_va_chuan_bi_du_lieu_moi
storage_ghi_du_lieu_moi = storage_ghi_them
