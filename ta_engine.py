"""
ta_engine.py
=============
Module 2: TECHNICAL ANALYSIS ENGINE - xử lý 100% bằng code Python thuần
(KHÔNG dùng LLM cho bước này, theo đúng kiến trúc Multi-modular Stock AI Agent).

Nâng cấp từ phan_tich_ky_thuat.py (Sprint 1), thêm:
- EMA_200 (bên cạnh EMA_20, EMA_50 đã có)
- Xác định vùng Hỗ trợ / Kháng cự dựa trên Swing High/Low (đỉnh/đáy lịch sử)
- Xác định xu hướng chính: Uptrend / Downtrend / Sideway (dựa trên EMA_20/50/200)
- Multi-factor Signal: tín hiệu MUA/BÁN chỉ được xác nhận khi ĐỦ 4 điều kiện cùng lúc
  (Breakout + Volume Spike + RSI không quá mua/bán + Vị thế giá so EMA_20)
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd
import pandas_ta as ta


# ==================== CẤU HÌNH ====================
SWING_WINDOW = 5           # số phiên mỗi bên để xác nhận 1 đỉnh/đáy cục bộ (swing high/low)
NGUONG_VOLUME_SPIKE = 1.5   # khối lượng > 1.5x MA20 mới tính là đột biến
# ====================================================


# ==================== CHỈ BÁO KỸ THUẬT (giữ nguyên từ Sprint 1, bổ sung EMA_200) ====================

def tinh_chi_bao_ky_thuat(df: pd.DataFrame) -> pd.DataFrame:
    """
    Nhận vào DataFrame OHLCV (cần có các cột: open, high, low, close, volume)
    của 1 mã cổ phiếu, đã sắp xếp theo thời gian tăng dần.

    Trả về DataFrame gốc + các cột chỉ báo kỹ thuật:
    - RSI_14, MACD_12_26_9 / MACDs_12_26_9, BBL/M/U_20_2.0
    - EMA_20 / EMA_50 / EMA_200  (EMA_200 cần >=200 phiên mới có giá trị, có thể NaN nếu thiếu dữ liệu)
    - volume_ma20 / volume_spike
    """
    df = df.copy()

    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.ema(length=200, append=True)  # MỚI: cần cho xác định xu hướng dài hạn

    df["volume_ma20"] = df["volume"].rolling(window=20).mean()
    df["volume_spike"] = df["volume"] > (df["volume_ma20"] * NGUONG_VOLUME_SPIKE)

    # QUAN TRỌNG: pandas_ta KHÔNG tạo cột chỉ báo nếu chưa đủ dữ liệu tối thiểu
    # (ví dụ: cần >=200 phiên mới có cột EMA_200, >=14 phiên mới có RSI_14...),
    # thay vì tạo cột toàn NaN như mong đợi. Với watchlist hiện tại mới có
    # ~110-150 phiên, hoặc mã mới thêm còn ít dữ liệu, đây là tình huống THỰC TẾ
    # sẽ xảy ra, không phải edge case lý thuyết. Đảm bảo mọi cột luôn tồn tại
    # (điền NaN nếu thiếu) để code phía sau (xac_dinh_tin_hieu, agent_brain.py...)
    # không bao giờ bị KeyError, dù dữ liệu ít đến đâu.
    cac_cot_bat_buoc = [
        "RSI_14", "MACD_12_26_9", "MACDs_12_26_9", "MACDh_12_26_9",
        "BBL_20_2.0", "BBM_20_2.0", "BBU_20_2.0",
        "EMA_20", "EMA_50", "EMA_200",
    ]
    for col in cac_cot_bat_buoc:
        if col not in df.columns:
            df[col] = float("nan")

    return df
# =========================================================


# ==================== SUPPORT / RESISTANCE (Swing High/Low) ====================

def xac_dinh_dinh_day(df: pd.DataFrame, window: int = SWING_WINDOW) -> pd.DataFrame:
    """
    Đánh dấu các đỉnh/đáy cục bộ (swing high / swing low):
    1 phiên được coi là đỉnh nếu giá high của nó là CAO NHẤT trong cửa sổ
    [window phiên trước, window phiên sau]. Tương tự cho đáy với giá low.

    LƯU Ý QUAN TRỌNG (tránh look-ahead bias): 1 đỉnh/đáy chỉ được XÁC NHẬN
    sau khi đã có đủ `window` phiên kế tiếp - tức `window` phiên gần nhất
    trong dữ liệu (bao gồm phiên hiện tại) sẽ luôn là NaN/False vì chưa đủ
    dữ liệu tương lai để xác nhận. Đây là hành vi ĐÚNG, không phải bug.
    """
    df = df.copy()
    cua_so = window * 2 + 1

    dinh_cuc_bo = df["high"].rolling(cua_so, center=True).max()
    day_cuc_bo = df["low"].rolling(cua_so, center=True).min()

    df["swing_high"] = df["high"] == dinh_cuc_bo
    df["swing_low"] = df["low"] == day_cuc_bo
    # Phiên nào rolling trả về NaN (do thiếu dữ liệu tương lai) -> chưa xác nhận được
    df.loc[dinh_cuc_bo.isna(), "swing_high"] = False
    df.loc[day_cuc_bo.isna(), "swing_low"] = False

    return df


def tinh_ho_tro_khang_cu_hien_tai(df: pd.DataFrame, so_muc_gan_nhat: int = 3) -> dict:
    """
    Từ các đỉnh/đáy đã xác nhận (cột swing_high/swing_low), xác định:
    - Kháng cự gần nhất: mức đỉnh cũ THẤP NHẤT nằm TRÊN giá hiện tại
    - Hỗ trợ gần nhất: mức đáy cũ CAO NHẤT nằm DƯỚI giá hiện tại
    - Danh sách `so_muc_gan_nhat` mức hỗ trợ/kháng cự gần đây nhất (theo thời gian) để tham khảo thêm

    Cần gọi xac_dinh_dinh_day(df) trước để có cột swing_high/swing_low.
    Trả về dict: {"ho_tro": float|None, "khang_cu": float|None,
                  "cac_muc_ho_tro": list, "cac_muc_khang_cu": list}
    """
    if df.empty or "swing_high" not in df.columns:
        return {"ho_tro": None, "khang_cu": None, "cac_muc_ho_tro": [], "cac_muc_khang_cu": []}

    gia_hien_tai = df["close"].iloc[-1]

    cac_dinh = df.loc[df["swing_high"], "high"]
    cac_day = df.loc[df["swing_low"], "low"]

    khang_cu_phia_tren = cac_dinh[cac_dinh > gia_hien_tai]
    ho_tro_phia_duoi = cac_day[cac_day < gia_hien_tai]

    khang_cu = khang_cu_phia_tren.min() if not khang_cu_phia_tren.empty else None
    ho_tro = ho_tro_phia_duoi.max() if not ho_tro_phia_duoi.empty else None

    return {
        "ho_tro": ho_tro,
        "khang_cu": khang_cu,
        "cac_muc_ho_tro": sorted(cac_day.tail(so_muc_gan_nhat).tolist(), reverse=True),
        "cac_muc_khang_cu": sorted(cac_dinh.tail(so_muc_gan_nhat).tolist()),
    }
# =========================================================


# ==================== XU HƯỚNG CHÍNH (dựa trên EMA 20/50/200) ====================

def xac_dinh_xu_huong(row: pd.Series) -> str:
    """
    Xác định xu hướng chính dựa trên vị trí tương đối của EMA_20/50/200:
    - UPTREND:  EMA_20 > EMA_50 > EMA_200  (các đường xếp thang tăng dần)
    - DOWNTREND: EMA_20 < EMA_50 < EMA_200
    - SIDEWAY:  các trường hợp còn lại (đan xen, chưa rõ xu hướng)

    Nếu EMA_200 chưa có giá trị (thiếu dữ liệu lịch sử, cần >=200 phiên),
    tự động fallback về so sánh EMA_20 vs EMA_50 và đánh dấu rõ là dữ liệu
    chưa đủ để xác nhận xu hướng dài hạn.
    """
    ema20, ema50, ema200 = row.get("EMA_20"), row.get("EMA_50"), row.get("EMA_200")

    if pd.isna(ema20) or pd.isna(ema50):
        return "CHUA_DU_DU_LIEU"

    if pd.isna(ema200):
        # Chưa đủ 200 phiên lịch sử -> chỉ xác định xu hướng ngắn/trung hạn
        if ema20 > ema50:
            return "UPTREND_NGAN_HAN (thiếu dữ liệu EMA_200)"
        elif ema20 < ema50:
            return "DOWNTREND_NGAN_HAN (thiếu dữ liệu EMA_200)"
        return "SIDEWAY (thiếu dữ liệu EMA_200)"

    if ema20 > ema50 > ema200:
        return "UPTREND"
    elif ema20 < ema50 < ema200:
        return "DOWNTREND"
    return "SIDEWAY"
# =========================================================


# ==================== MULTI-FACTOR SIGNAL ====================

def xac_dinh_tin_hieu(df: pd.DataFrame, ho_tro: float, khang_cu: float) -> pd.DataFrame:
    """
    Thêm cột 'tin_hieu' (MUA / BAN / GIU) dựa trên MULTI-FACTOR CONFIRMATION -
    chỉ phát tín hiệu khi ĐỦ CẢ 4 điều kiện cùng lúc (không dùng 1 chỉ báo đơn lẻ,
    tránh tín hiệu nhiễu):

    MUA cần ĐỦ 4 điều kiện:
      1. Price Breakout : giá đóng cửa vượt LÊN trên mức kháng cự gần nhất
      2. Volume Spike    : khối lượng phiên > 1.5x trung bình 20 phiên
      3. RSI hợp lệ      : RSI_14 < 70 (chưa vào vùng quá mua)
      4. Vị thế giá      : giá đóng cửa đang > EMA_20 (xác nhận đà tăng ngắn hạn)

    BÁN cần ĐỦ 4 điều kiện (đối xứng):
      1. Price Breakdown : giá đóng cửa vượt XUỐNG dưới mức hỗ trợ gần nhất
      2. Volume Spike     : khối lượng phiên > 1.5x trung bình 20 phiên
      3. RSI hợp lệ       : RSI_14 > 30 (chưa vào vùng quá bán)
      4. Vị thế giá       : giá đóng cửa đang < EMA_20

    Cần gọi tinh_chi_bao_ky_thuat(df) trước để có RSI_14, EMA_20, volume_spike.
    ho_tro/khang_cu lấy từ tinh_ho_tro_khang_cu_hien_tai(df).
    """
    df = df.copy()

    breakout_len = df["close"] > khang_cu if khang_cu is not None else pd.Series(False, index=df.index)
    breakout_xuong = df["close"] < ho_tro if ho_tro is not None else pd.Series(False, index=df.index)

    dieu_kien_mua = (
        breakout_len
        & df["volume_spike"]
        & (df["RSI_14"] < 70)
        & (df["close"] > df["EMA_20"])
    )
    dieu_kien_ban = (
        breakout_xuong
        & df["volume_spike"]
        & (df["RSI_14"] > 30)
        & (df["close"] < df["EMA_20"])
    )

    df["tin_hieu"] = "GIU"
    df.loc[dieu_kien_mua, "tin_hieu"] = "MUA"
    df.loc[dieu_kien_ban, "tin_hieu"] = "BAN"  # nếu cả 2 đều đúng cùng lúc (hiếm), ưu tiên BÁN

    return df
# =========================================================


# ==================== HÀM TỔNG HỢP CHO 1 MÃ ====================

def phan_tich_mot_ma(df_all: pd.DataFrame, ma: str) -> dict:
    """
    Lọc dữ liệu của 1 mã từ DataFrame tổng, tính đầy đủ: chỉ báo kỹ thuật,
    đỉnh/đáy, hỗ trợ/kháng cự, xu hướng, và tín hiệu multi-factor.

    Trả về dict gồm:
    - "df": DataFrame đầy đủ (mọi cột chỉ báo + tin_hieu, dùng cho agent_brain.py sau này)
    - "ho_tro", "khang_cu": mức giá hiện tại
    - "xu_huong": chuỗi mô tả xu hướng của phiên gần nhất
    - "tin_hieu_gan_nhat": MUA/BAN/GIU của phiên gần nhất
    """
    df_ma = df_all[df_all["ma"] == ma].copy().sort_values("time").reset_index(drop=True)

    df_ma = tinh_chi_bao_ky_thuat(df_ma)
    df_ma = xac_dinh_dinh_day(df_ma)

    sr = tinh_ho_tro_khang_cu_hien_tai(df_ma)
    df_ma = xac_dinh_tin_hieu(df_ma, sr["ho_tro"], sr["khang_cu"])

    xu_huong = xac_dinh_xu_huong(df_ma.iloc[-1]) if not df_ma.empty else "KHONG_CO_DU_LIEU"

    return {
        "df": df_ma,
        "ho_tro": sr["ho_tro"],
        "khang_cu": sr["khang_cu"],
        "cac_muc_ho_tro": sr["cac_muc_ho_tro"],
        "cac_muc_khang_cu": sr["cac_muc_khang_cu"],
        "xu_huong": xu_huong,
        "tin_hieu_gan_nhat": df_ma["tin_hieu"].iloc[-1] if not df_ma.empty else None,
    }
# =========================================================


# ==================== ALIAS TUONG THICH NGUOC ====================
# GHI CHU: agent_brain.py (phien ban 22:49 8/7) goi ham
# phan_tich_ky_thuat_toan_dien(ma, df_all, df_vnindex) nhung ham nay chua
# ton tai trong file nay - phat hien loi "module co attribute" khi chay thuc te.
# Them alias mong (thin wrapper) quanh phan_tich_mot_ma() da co san va da test,
# CHUA thay doi logic goc, tranh xung dot voi phien dang chinh sua file nay
# song song. Neu sau nay can dung that su df_vnindex (VD: so sanh xu huong
# voi VN-Index), co the mo rong ham nay sau - hien tai chi nhan de tuong
# thich chu ky ham, chua su dung.
def phan_tich_ky_thuat_toan_dien(ma: str, df_all: pd.DataFrame, df_vnindex: pd.DataFrame = None) -> dict:
    """Alias tuong thich nguoc cho phan_tich_mot_ma() - xem ham do de biet chi tiet."""
    return phan_tich_mot_ma(df_all, ma)
# =====================================================================


# --- TEST NHANH ---
if __name__ == "__main__":
    import os

    csv_path = r"C:\vn_stock_agent_data\lich_su_gia.csv"

    if not os.path.exists(csv_path):
        print(f"❌ Không tìm thấy file dữ liệu: {csv_path}")
    else:
        df_all = pd.read_csv(csv_path)

        danh_sach_ma = ["TCB", "BID", "ACB", "MBB"]
        cot_can_xem = ["time", "close", "RSI_14", "EMA_20", "EMA_50", "EMA_200", "tin_hieu"]

        for ma in danh_sach_ma:
            if ma not in df_all["ma"].unique():
                print(f"❌ Chưa có dữ liệu cho mã {ma}")
                continue

            kq = phan_tich_mot_ma(df_all, ma)
            df_ket_qua = kq["df"]
            cot_co_san = [c for c in cot_can_xem if c in df_ket_qua.columns]

            print(f"\n{'='*70}\n📊 {ma}\n{'='*70}")
            print(df_ket_qua[cot_co_san].tail(8).to_string(index=False))

            print(f"\n📈 Xu hướng hiện tại: {kq['xu_huong']}")
            print(f"🟢 Hỗ trợ gần nhất : {kq['ho_tro']}")
            print(f"🔴 Kháng cự gần nhất: {kq['khang_cu']}")
            print(f"🎯 Tín hiệu phiên gần nhất: {kq['tin_hieu_gan_nhat']}")

            tin_hieu_gan_nhat = df_ket_qua[df_ket_qua["tin_hieu"] != "GIU"].tail(1)
            if not tin_hieu_gan_nhat.empty:
                dong = tin_hieu_gan_nhat.iloc[0]
                print(f"⚡ Tín hiệu MUA/BÁN xác nhận gần nhất: {dong['tin_hieu']} vào ngày {dong['time']} (giá đóng cửa: {dong['close']})")
            else:
                print("⚡ Chưa có tín hiệu MUA/BÁN xác nhận (đủ multi-factor) nào trong dữ liệu hiện có.")