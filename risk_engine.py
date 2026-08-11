"""
risk_engine.py
===============
Module 5: RISK MANAGEMENT & VN-MARKET RULES - xử lý 100% bằng code Python thuần.

Chức năng:
- Tính điểm giao dịch: Entry / Stop Loss (ATR hoặc hỗ trợ gần nhất) / Take Profit (kháng cự, đảm bảo R:R >= 1:2)
- Ràng buộc biên độ giao dịch sàn VN (+/-7% HOSE, +/-10% HNX)
- Rủi ro T+2.5: hạ confidence_score nếu tín hiệu MUA xuất hiện cuối phiên hoặc sát kháng cự mạnh
- Beta & Max Drawdown: đo mức biến động của cổ phiếu so với VN-Index

LƯU Ý: module này KHÔNG gọi LLM, chỉ tính toán bằng công thức tài chính thuần.
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')

from datetime import datetime, time as dt_time
import pandas as pd
import pandas_ta as ta


# ==================== CẤU HÌNH ====================
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 2.0        # Stop Loss = Entry - 2 * ATR
MIN_RR_RATIO = 2.0             # Take Profit tối thiểu phải đạt R:R >= 1:2

BIEN_DO_HOSE = 0.07            # +/-7%
BIEN_DO_HNX = 0.10             # +/-10%

# Toàn bộ 20 mã trong watchlist đều niêm yết trên HOSE (đã verify qua tin tức thị trường
# 08/2026: 18/18 cổ phiếu ngân hàng trong watchlist thuộc HoSE, không mã nào thuộc HNX).
# Nếu sau này thêm mã mới vào watchlist, PHẢI cập nhật dict này trước khi tin tưởng kết quả.
SAN_GIAO_DICH = {
    ma: "HOSE"
    for ma in [
        "VCB", "BID", "CTG", "TCB", "VPB", "MBB", "ACB", "HDB", "STB",
        "SHB", "VRE", "TPB", "OCB", "VIB", "LPB",
        "HPG", "FPT", "MSN", "VIC", "MWG",
    ]
}

GIO_CANH_BAO_T25 = dt_time(14, 0)   # sau 14:00 -> cảnh báo rủi ro T+2.5
MUC_GIAM_CONFIDENCE_T25 = 18        # % giảm confidence_score khi dính rủi ro T+2.5 (trong khoảng 15-20% theo yêu cầu)
NGUONG_SAT_KHANG_CU_PCT = 0.02      # entry cách kháng cự <2% coi là "sát kháng cự mạnh"
# ====================================================


# ==================== ATR ====================

def tinh_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.DataFrame:
    """
    Thêm cột ATR (Average True Range) vào df - đo độ biến động trung bình,
    dùng để đặt Stop Loss theo biến động thực tế của từng mã (không cố định %).
    Cần cột high, low, close.
    """
    df = df.copy()
    df.ta.atr(length=period, append=True)
    col = f"ATRr_{period}"
    if col not in df.columns:
        df[col] = float("nan")
    df["ATR"] = df[col]
    return df
# =========================================================


# ==================== ENTRY / STOP LOSS / TAKE PROFIT ====================

def tinh_entry_sl_tp(df_ta: pd.DataFrame, ho_tro: float, khang_cu: float, action: str) -> dict:
    """
    Tính điểm giao dịch dựa trên ATR + vùng hỗ trợ/kháng cự.
    df_ta cần đã có cột 'ATR' (gọi tinh_atr trước) và 'close'.

    MUA:
      - Entry   = giá đóng cửa hiện tại
      - SL      = giá trị LỚN HƠN (tức gần Entry hơn, rủi ro thấp hơn) giữa:
                  (Entry - 2*ATR) và mức hỗ trợ gần nhất
      - TP      = mức kháng cự gần nhất, NẾU nó đạt R:R >= 1:2; nếu không,
                  tự tính TP = Entry + 2 * (Entry - SL) để đảm bảo tối thiểu 1:2

    BÁN (chỉ mang tính tham khảo - thị trường VN không hỗ trợ short-sell đại chúng):
      - Đối xứng ngược lại với MUA.

    Nếu thiếu ATR (NaN, do dữ liệu quá ngắn) hoặc thiếu hỗ trợ/kháng cự,
    trả về dict với các trường None và ghi rõ lý do trong "ly_do_khong_tinh_duoc".
    """
    if df_ta.empty:
        return _ket_qua_rong("Không có dữ liệu")

    dong_cuoi = df_ta.iloc[-1]
    entry = dong_cuoi.get("close")
    atr = dong_cuoi.get("ATR")

    if pd.isna(entry):
        return _ket_qua_rong("Thiếu giá đóng cửa")
    if pd.isna(atr):
        return _ket_qua_rong("Thiếu ATR (dữ liệu chưa đủ để tính độ biến động)")

    if action == "MUA":
        sl_theo_atr = entry - ATR_SL_MULTIPLIER * atr
        sl_theo_ho_tro = ho_tro if ho_tro is not None else sl_theo_atr
        stop_loss = max(sl_theo_atr, sl_theo_ho_tro)
        # Đảm bảo SL luôn thấp hơn Entry (phòng trường hợp hỗ trợ tính lệch do dữ liệu nhiễu)
        if stop_loss >= entry:
            stop_loss = sl_theo_atr

        rui_ro = entry - stop_loss
        tp_toi_thieu = entry + MIN_RR_RATIO * rui_ro
        take_profit = khang_cu if (khang_cu is not None and khang_cu >= tp_toi_thieu) else tp_toi_thieu

    elif action == "BAN":
        sl_theo_atr = entry + ATR_SL_MULTIPLIER * atr
        sl_theo_khang_cu = khang_cu if khang_cu is not None else sl_theo_atr
        stop_loss = min(sl_theo_atr, sl_theo_khang_cu)
        if stop_loss <= entry:
            stop_loss = sl_theo_atr

        rui_ro = stop_loss - entry
        tp_toi_thieu = entry - MIN_RR_RATIO * rui_ro
        take_profit = ho_tro if (ho_tro is not None and ho_tro <= tp_toi_thieu) else tp_toi_thieu

    else:  # GIU - không tính điểm giao dịch
        return _ket_qua_rong("Tín hiệu GIU - không cần tính điểm giao dịch")

    rui_ro = abs(entry - stop_loss)
    loi_nhuan_ky_vong = abs(take_profit - entry)
    rr_ratio = (loi_nhuan_ky_vong / rui_ro) if rui_ro > 0 else None

    return {
        "entry": round(entry, 2),
        "stop_loss": round(stop_loss, 2),
        "take_profit": round(take_profit, 2),
        "risk_reward_ratio": round(rr_ratio, 2) if rr_ratio is not None else None,
        "atr": round(atr, 2),
        "ly_do_khong_tinh_duoc": None,
    }


def _ket_qua_rong(ly_do: str) -> dict:
    return {
        "entry": None, "stop_loss": None, "take_profit": None,
        "risk_reward_ratio": None, "atr": None,
        "ly_do_khong_tinh_duoc": ly_do,
    }
# =========================================================


# ==================== BIÊN ĐỘ GIAO DỊCH SÀN ====================

def kiem_tra_bien_do_san(entry: float, stop_loss: float, take_profit: float,
                          gia_tham_chieu: float, ma: str) -> dict:
    """
    VN-Market Rule: TP và SL bắt buộc nằm trong biên độ cho phép của sàn
    (+/-7% HOSE, +/-10% HNX) so với giá tham chiếu (thường là giá đóng cửa
    phiên trước). Nếu vượt biên độ, tự động điều chỉnh về sát trần/sàn
    (thay vì để agent đưa ra mức giá không thể khớp lệnh thực tế).

    Trả về dict: {"stop_loss", "take_profit" (đã điều chỉnh nếu cần),
                  "gia_tran", "gia_san", "da_dieu_chinh": bool, "canh_bao": list}
    """
    san = SAN_GIAO_DICH.get(ma, "HOSE")  # mặc định HOSE nếu mã lạ chưa có trong mapping
    bien_do = BIEN_DO_HOSE if san == "HOSE" else BIEN_DO_HNX

    gia_tran = round(gia_tham_chieu * (1 + bien_do), 2)
    gia_san = round(gia_tham_chieu * (1 - bien_do), 2)

    canh_bao = []
    da_dieu_chinh = False

    tp_dieu_chinh = take_profit
    sl_dieu_chinh = stop_loss

    if take_profit is not None and take_profit > gia_tran:
        tp_dieu_chinh = gia_tran
        da_dieu_chinh = True
        canh_bao.append(f"Take Profit ({take_profit}) vượt giá trần {san} ({gia_tran}) - đã điều chỉnh về giá trần")

    if stop_loss is not None and stop_loss < gia_san:
        sl_dieu_chinh = gia_san
        da_dieu_chinh = True
        canh_bao.append(f"Stop Loss ({stop_loss}) thấp hơn giá sàn {san} ({gia_san}) - đã điều chỉnh về giá sàn")

    return {
        "stop_loss": sl_dieu_chinh,
        "take_profit": tp_dieu_chinh,
        "gia_tran": gia_tran,
        "gia_san": gia_san,
        "san_giao_dich": san,
        "da_dieu_chinh": da_dieu_chinh,
        "canh_bao": canh_bao,
    }
# =========================================================


# ==================== RỦI RO T+2.5 ====================

def kiem_tra_rui_ro_t25(thoi_gian_tin_hieu: datetime, entry: float, khang_cu: float) -> dict:
    """
    VN-Market Rule: Rủi ro T+2.5 (cổ phiếu mua về sau 2.5 ngày mới bán được).
    Nếu tín hiệu MUA xuất hiện:
      - Sau 14:00 (cuối phiên, dễ mua đúng đỉnh ngắn hạn trong phiên), HOẶC
      - Giá entry đã rất sát kháng cự mạnh (<2%, dễ bị đảo chiều ngay sau khi mua)
    -> tự động hạ confidence_score xuống 15-20% và cảnh báo rủi ro kẹt hàng T+2.5.

    Trả về dict: {"giam_confidence_pct": int, "canh_bao": list}
    """
    canh_bao = []
    giam_confidence_pct = 0

    if thoi_gian_tin_hieu.time() >= GIO_CANH_BAO_T25:
        canh_bao.append(
            f"Tín hiệu xuất hiện sau {GIO_CANH_BAO_T25.strftime('%H:%M')} - "
            f"rủi ro mua đúng vùng giá cao cuối phiên, kẹt hàng T+2.5"
        )
        giam_confidence_pct = MUC_GIAM_CONFIDENCE_T25

    if khang_cu is not None and entry is not None and khang_cu > 0:
        khoang_cach_pct = (khang_cu - entry) / khang_cu
        if 0 <= khoang_cach_pct < NGUONG_SAT_KHANG_CU_PCT:
            canh_bao.append(
                f"Giá entry ({entry}) chỉ cách kháng cự mạnh ({khang_cu}) "
                f"{khoang_cach_pct*100:.1f}% - rủi ro đảo chiều ngay sau khi mua, kẹt hàng T+2.5"
            )
            giam_confidence_pct = max(giam_confidence_pct, MUC_GIAM_CONFIDENCE_T25)

    return {"giam_confidence_pct": giam_confidence_pct, "canh_bao": canh_bao}
# =========================================================


# ==================== BETA & MAX DRAWDOWN ====================

def tinh_beta(df_ma: pd.DataFrame, df_vnindex: pd.DataFrame) -> float | None:
    """
    Beta = Cov(lợi nhuận mã, lợi nhuận VN-Index) / Var(lợi nhuận VN-Index).
    Beta > 1: biến động mạnh hơn thị trường. Beta < 1: ổn định hơn thị trường.
    Cần 2 DataFrame đã sort theo thời gian tăng dần, có cột 'time' và 'close'.
    """
    if df_ma.empty or df_vnindex.empty:
        return None

    hop_nhat = pd.merge(
        df_ma[["time", "close"]].rename(columns={"close": "close_ma"}),
        df_vnindex[["time", "close"]].rename(columns={"close": "close_index"}),
        on="time", how="inner",
    )
    if len(hop_nhat) < 10:  # quá ít điểm chung để tính beta có ý nghĩa
        return None

    hop_nhat["return_ma"] = hop_nhat["close_ma"].pct_change()
    hop_nhat["return_index"] = hop_nhat["close_index"].pct_change()
    hop_nhat = hop_nhat.dropna()

    phuong_sai_index = hop_nhat["return_index"].var()
    if phuong_sai_index == 0 or pd.isna(phuong_sai_index):
        return None

    hiep_phuong_sai = hop_nhat["return_ma"].cov(hop_nhat["return_index"])
    return round(hiep_phuong_sai / phuong_sai_index, 2)


def tinh_max_drawdown(df_ma: pd.DataFrame) -> float | None:
    """
    Max Drawdown: mức sụt giảm lớn nhất từ đỉnh xuống đáy trong toàn bộ giai đoạn
    dữ liệu hiện có, tính theo %. Số càng âm càng rủi ro.
    """
    if df_ma.empty or "close" not in df_ma.columns:
        return None
    gia = df_ma["close"]
    dinh_tich_luy = gia.cummax()
    drawdown = (gia - dinh_tich_luy) / dinh_tich_luy
    return round(drawdown.min() * 100, 2)  # %
# =========================================================


# ==================== HÀM TỔNG HỢP ====================

def danh_gia_rui_ro_day_du(
    df_ta: pd.DataFrame,
    ma: str,
    action: str,
    ho_tro: float,
    khang_cu: float,
    thoi_gian_tin_hieu: datetime = None,
    df_vnindex: pd.DataFrame = None,
) -> dict:
    """
    Hàm tổng hợp: tính Entry/SL/TP -> áp biên độ sàn -> kiểm tra rủi ro T+2.5
    -> tính Beta/Max Drawdown (nếu có dữ liệu VN-Index). Dùng hàm này từ bên ngoài
    thay vì gọi từng hàm lẻ, trừ khi cần custom logic riêng.
    """
    if thoi_gian_tin_hieu is None:
        thoi_gian_tin_hieu = datetime.now()

    df_ta = tinh_atr(df_ta)
    diem_giao_dich = tinh_entry_sl_tp(df_ta, ho_tro, khang_cu, action)

    ket_qua = {
        "ma": ma,
        "action": action,
        **diem_giao_dich,
        "canh_bao": [],
        "giam_confidence_pct": 0,
        "beta": None,
        "max_drawdown_pct": None,
    }

    if diem_giao_dich["entry"] is not None:
        gia_tham_chieu = df_ta["close"].iloc[-2] if len(df_ta) >= 2 else df_ta["close"].iloc[-1]
        bien_do = kiem_tra_bien_do_san(
            diem_giao_dich["entry"], diem_giao_dich["stop_loss"],
            diem_giao_dich["take_profit"], gia_tham_chieu, ma,
        )
        ket_qua["stop_loss"] = bien_do["stop_loss"]
        ket_qua["take_profit"] = bien_do["take_profit"]
        ket_qua["gia_tran"] = bien_do["gia_tran"]
        ket_qua["gia_san"] = bien_do["gia_san"]
        ket_qua["san_giao_dich"] = bien_do["san_giao_dich"]
        ket_qua["canh_bao"].extend(bien_do["canh_bao"])

        if action == "MUA":
            t25 = kiem_tra_rui_ro_t25(thoi_gian_tin_hieu, diem_giao_dich["entry"], khang_cu)
            ket_qua["giam_confidence_pct"] = t25["giam_confidence_pct"]
            ket_qua["canh_bao"].extend(t25["canh_bao"])

    df_ma_sorted = df_ta.sort_values("time") if "time" in df_ta.columns else df_ta
    ket_qua["max_drawdown_pct"] = tinh_max_drawdown(df_ma_sorted)
    if df_vnindex is not None:
        ket_qua["beta"] = tinh_beta(df_ma_sorted, df_vnindex)

    return ket_qua
# =========================================================


# ==================== ALIAS TUONG THICH NGUOC ====================
# GHI CHU: agent_brain.py (phien ban dang duoc 1 session khac chinh sua song
# song) goi ham danh_gia_rui_ro_toan_dien(ma, kq_ta) - nhan truc tiep dict
# output cua ta_engine.phan_tich_ky_thuat_toan_dien() thay vi tung tham so
# rieng le. Them alias mong de tuong thich, KHONG doi logic goc cua
# danh_gia_rui_ro(). LUU Y: cach goi nay KHONG truyen df_vnindex vao, nen
# Beta se tra ve None (thieu du lieu VNINDEX de tinh) - day la gioi han cua
# cach goi tu agent_brain.py hien tai, khong phai loi alias nay.
def danh_gia_rui_ro_toan_dien(ma: str, kq_ta: dict, df_vnindex=None, gio_hien_tai: int = None) -> dict:
    """
    Alias tuong thich nguoc cho danh_gia_rui_ro_day_du() - nhan truc tiep output
    cua ta_engine.phan_tich_ky_thuat_toan_dien() (dict co "df", "ho_tro",
    "khang_cu", "tin_hieu_gan_nhat") thay vi tung tham so rieng le.
    CAP NHAT: ham that trong file nay la danh_gia_rui_ro_day_du(), khong phai
    danh_gia_rui_ro() nhu ban dau - da doi lai cho dung voi phien ban hien tai.
    """
    action = kq_ta.get("tin_hieu_gan_nhat") or "GIU"
    return danh_gia_rui_ro_day_du(
        df_ta=kq_ta["df"],
        ma=ma,
        action=action,
        ho_tro=kq_ta.get("ho_tro"),
        khang_cu=kq_ta.get("khang_cu"),
        df_vnindex=df_vnindex,
    )
# =====================================================================


# --- TEST NHANH ---
if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ta_engine as te

    csv_path = r"C:\vn_stock_agent_data\lich_su_gia.csv"

    if not os.path.exists(csv_path):
        print(f"❌ Không tìm thấy file dữ liệu: {csv_path}")
    else:
        df_all = pd.read_csv(csv_path)
        df_vnindex_raw = df_all[df_all["ma"] == "VNINDEX"].sort_values("time")

        for ma in ["TCB", "BID", "ACB", "MBB"]:
            if ma not in df_all["ma"].unique():
                print(f"❌ Chưa có dữ liệu cho mã {ma}")
                continue

            kq_ta = te.phan_tich_mot_ma(df_all, ma)
            df_ta, ho_tro, khang_cu = kq_ta["df"], kq_ta["ho_tro"], kq_ta["khang_cu"]
            action = kq_ta["tin_hieu_gan_nhat"] if kq_ta["tin_hieu_gan_nhat"] != "GIU" else "MUA"  # ép test cả khi tín hiệu là GIU

            kq_risk = danh_gia_rui_ro_day_du(
                df_ta, ma, action, ho_tro, khang_cu,
                df_vnindex=df_vnindex_raw if not df_vnindex_raw.empty else None,
            )

            print(f"\n{'='*70}\n💰 {ma} - Risk Assessment (giả lập action={action})\n{'='*70}")
            for k, v in kq_risk.items():
                print(f"{k}: {v}")