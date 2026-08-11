import sys
sys.stdout.reconfigure(encoding='utf-8')

from datetime import datetime, timedelta
import time

from vnstock.ui import Market

# Tái sử dụng lại các hàm và biến đã viết trong data_engine.py
# (đúng tinh thần "hàm" đã học - không viết lại logic đã có)
from data_engine import (
    WATCHLIST,
    retry_on_rate_limit,
    storage_nap_du_lieu_da_luu,
    chuan_hoa_va_loc_dong_moi,
    storage_ghi_du_lieu_moi,
)

SO_NGAY_BACKFILL = 200   # đủ dữ liệu để tính RSI(14), MACD(12,26,9), EMA(50), BBands(20)


def ghi_neu_co_dong_moi(df, ma, da_luu) -> int:
    """Chuẩn hoá + lọc trùng + ghi vào storage. Trả về số dòng mới đã ghi."""
    df_moi = chuan_hoa_va_loc_dong_moi(df, ma, da_luu)
    if df_moi.empty:
        return 0
    if not storage_ghi_du_lieu_moi(df_moi):
        return 0
    for _, r in df_moi.iterrows():
        da_luu.add((r["ma"], r["time"]))
    return len(df_moi)


def main():
    print(f"🚀 Bắt đầu tải lịch sử {SO_NGAY_BACKFILL} ngày cho {len(WATCHLIST)} mã...")
    print("🛑 Có thể mất 1-2 phút, đừng tắt cửa sổ giữa chừng.\n")

    mkt = Market()
    da_luu = storage_nap_du_lieu_da_luu()
    ngay_bat_dau = (datetime.now() - timedelta(days=SO_NGAY_BACKFILL)).strftime("%Y-%m-%d")
    ngay_ket_thuc = datetime.now().strftime("%Y-%m-%d")

    tong_dong_moi = 0

    # 1. VN-INDEX
    try:
        print("📥 Đang tải VN-Index...")
        vnindex_df = retry_on_rate_limit(
            lambda: mkt.index("VNINDEX").ohlcv(start=ngay_bat_dau, end=ngay_ket_thuc),
            label="VNINDEX"
        )
        so_dong = ghi_neu_co_dong_moi(vnindex_df, "VNINDEX", da_luu)
        tong_dong_moi += so_dong
        print(f"✅ VN-Index: {so_dong} phiên mới được lưu")
    except Exception as e:
        print(f"❌ Lỗi khi tải VN-Index: {e}")
    finally:
        time.sleep(4)

    # 2. TỪNG MÃ TRONG WATCHLIST
    for ma_cp in WATCHLIST:
        try:
            print(f"📥 Đang tải {ma_cp}...")
            df = retry_on_rate_limit(
                lambda: mkt.equity(ma_cp).ohlcv(start=ngay_bat_dau, end=ngay_ket_thuc),
                label=ma_cp
            )
            so_dong = ghi_neu_co_dong_moi(df, ma_cp, da_luu)
            tong_dong_moi += so_dong
            print(f"✅ {ma_cp}: {so_dong} phiên mới được lưu")
        except Exception as e:
            print(f"❌ Lỗi khi tải dữ liệu cổ phiếu {ma_cp}: {e}")
        finally:
            time.sleep(4)

    print(f"\n📊 Hoàn tất backfill: tổng cộng {tong_dong_moi} phiên mới được lưu vào CSV.")


if __name__ == "__main__":
    main()