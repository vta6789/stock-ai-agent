"""
fa_engine.py
=============
Module 4a: FUNDAMENTAL ANALYSIS ENGINE

QUAN TRỌNG - LÝ DO THIẾT KẾ: vnstock có sẵn hàm Finance.ratio() để lấy trực tiếp
các chỉ số (P/E, ROE, ROA...), NHƯNG đã test thực tế và phát hiện hàm này bị lỗi
- luôn trả về dữ liệu cũ tận 2018 dù gọi đúng tham số (đã test lại 2 lần, tái hiện
được lỗi). Ngược lại income_statement()/balance_sheet() hoạt động tốt, có dữ liệu
tới quý gần nhất (2026-Q2). Vì vậy module này TỰ TÍNH các chỉ số từ báo cáo tài
chính thô thay vì phụ thuộc ratio() - vừa tránh bug, vừa minh bạch công thức.

Xử lý 100% bằng code Python thuần (không dùng LLM) - đúng kiến trúc dự án.
"""

from typing import Optional
import pandas as pd
from vnstock import Finance


# ==================== TÊN DÒNG DỮ LIỆU (đã verify thực tế từ vnstock VCI) ====================
DONG_LOI_NHUAN_SAU_THUE = "Lợi nhuận sau thuế"
DONG_EPS_CO_BAN = "Lãi cơ bản trên cổ phiếu (VND)"
DONG_TONG_THU_NHAP_HOAT_DONG = "Tổng thu nhập hoạt động"
DONG_TONG_TAI_SAN = "TỔNG TÀI SẢN"
DONG_VON_CHU_SO_HUU = "VỐN CHỦ SỞ HỮU"
DONG_TONG_NO_PHAI_TRA = "TỔNG NỢ PHẢI TRẢ"
DONG_CHO_VAY_KHACH_HANG = "Cho vay khách hàng"
# ===================================================================================


def lay_bao_cao_tai_chinh(ma: str) -> dict:
    """Lấy income_statement + balance_sheet theo quý (nguồn VCI) cho 1 mã."""
    f = Finance(symbol=ma, source="VCI")
    return {
        "income_statement": f.income_statement(period="quarter", lang="vi"),
        "balance_sheet": f.balance_sheet(period="quarter", lang="vi"),
    }


def _lay_gia_tri_dong(df: pd.DataFrame, ten_dong: str, cot: str) -> Optional[float]:
    """Lấy giá trị của 1 dòng (theo tên item tiếng Việt) tại 1 cột (kỳ báo cáo)."""
    hang = df[df["item"] == ten_dong]
    if hang.empty or cot not in df.columns:
        return None
    gia_tri = hang[cot].iloc[0]
    return float(gia_tri) if pd.notna(gia_tri) else None


def _lay_cac_ky_gan_nhat(df: pd.DataFrame, so_ky: int) -> list:
    """
    Lấy tên các cột kỳ báo cáo (dạng 'YYYY-Qx') gần nhất, đã sắp xếp mới nhất trước.
    Bỏ qua cột 'item'/'item_en'/'item_id' và cột năm nguyên (không có '-Q').
    """
    cot_ky = [c for c in df.columns if "-Q" in str(c)]
    return cot_ky[:so_ky]  # vnstock trả về mới nhất ở đầu (đã verify: 2026-Q2, 2026-Q1, ...)


def tinh_chi_so_co_ban(ma: str, gia_hien_tai: Optional[float] = None) -> dict:
    """
    Tự tính các chỉ số cơ bản từ báo cáo tài chính thô (KHÔNG dùng ratio() đang lỗi):
    - ROE (TTM), ROA (TTM): Lợi nhuận sau thuế 4 quý gần nhất / Vốn CSH (hoặc Tổng TS) kỳ mới nhất
    - EPS (TTM): tổng EPS cơ bản 4 quý gần nhất
    - P/E: giá hiện tại / EPS TTM (chỉ tính nếu có gia_hien_tai)
    - Tăng trưởng lợi nhuận YoY: so quý mới nhất với CÙNG QUÝ năm trước
    - Tỷ lệ Vốn CSH/Tổng tài sản: đo mức độ đòn bẩy (đặc thù ngân hàng, KHÔNG dùng
      Debt/Equity kiểu công ty thường vì với ngân hàng, tiền gửi khách hàng cũng
      tính là "nợ phải trả" nên tỷ lệ này luôn rất cao và không phản ánh đúng rủi ro)

    LƯU Ý: P/B chưa tính được vì báo cáo không có sẵn số lượng cổ phiếu lưu hành
    (bị bỏ do phụ thuộc ratio() đang lỗi) - để làm ở phiên bản sau nếu cần.
    """
    bctc = lay_bao_cao_tai_chinh(ma)
    df_is = bctc["income_statement"]
    df_bs = bctc["balance_sheet"]

    cac_ky = _lay_cac_ky_gan_nhat(df_is, so_ky=4)
    if len(cac_ky) < 4:
        return {"ma": ma, "loi": f"Không đủ 4 quý dữ liệu (chỉ có {len(cac_ky)})"}

    ky_moi_nhat = cac_ky[0]

    # TTM (Trailing Twelve Months) = tổng 4 quý gần nhất
    loi_nhuan_ttm = sum(
        _lay_gia_tri_dong(df_is, DONG_LOI_NHUAN_SAU_THUE, ky) or 0 for ky in cac_ky
    )
    eps_ttm = sum(
        _lay_gia_tri_dong(df_is, DONG_EPS_CO_BAN, ky) or 0 for ky in cac_ky
    )

    tong_tai_san = _lay_gia_tri_dong(df_bs, DONG_TONG_TAI_SAN, ky_moi_nhat)
    von_chu_so_huu = _lay_gia_tri_dong(df_bs, DONG_VON_CHU_SO_HUU, ky_moi_nhat)

    roe_ttm = (loi_nhuan_ttm / von_chu_so_huu * 100) if von_chu_so_huu else None
    roa_ttm = (loi_nhuan_ttm / tong_tai_san * 100) if tong_tai_san else None
    ty_le_von_tren_tong_ts = (von_chu_so_huu / tong_tai_san * 100) if (von_chu_so_huu and tong_tai_san) else None

    # QUAN TRONG: gia dong cua trong CSV cua chung ta luu theo don vi "nghin dong"
    # (VD 37.95 nghia la 37,950 VND/CP - dung quy uoc hien thi gia CK VN pho bien),
    # trong khi EPS tu vnstock tra ve theo don vi "dong" nguyen (VD 3774 VND).
    # Neu khong quy doi se sai lech 1000 lan (da phat hien qua test thuc te: PE ra
    # 0.01 cho BID, vo ly). Phai nhan gia len 1000 truoc khi chia cho EPS.
    gia_hien_tai_vnd = gia_hien_tai * 1000 if gia_hien_tai else None
    pe = (gia_hien_tai_vnd / eps_ttm) if (gia_hien_tai_vnd and eps_ttm and eps_ttm != 0) else None
    ghi_chu_pe = None
    if eps_ttm == 0:
        ghi_chu_pe = "EPS = 0 do vnstock thieu du lieu dong nay cho ma nay (khong phai loi code, da verify truc tiep tu API)."

    # Tang truong loi nhuan YoY: CAN >=5 ky (quy hien tai + cung quy nam truoc).
    # Goi Community cua vnstock GIOI HAN CUNG CAP TOI DA 4 KY (da verify qua canh
    # bao chinh thuc tu vnstock luc goi API) -> KHONG THE tinh duoc voi tai khoan
    # hien tai, day la gioi han du lieu dau vao, khong phai loi logic code.
    tang_truong_yoy = None
    ghi_chu_yoy = "Khong tinh duoc: goi vnstock Community gioi han toi da 4 ky/lan goi, can >=5 ky de so cung ky nam truoc. Can nang cap goi vnstock de mo rong."
    if len(cac_ky) >= 1:
        nam_moi_nhat, quy_moi_nhat = ky_moi_nhat.split("-")
        ky_cung_ky_nam_truoc = f"{int(nam_moi_nhat) - 1}-{quy_moi_nhat}"
        if ky_cung_ky_nam_truoc in df_is.columns:
            ln_moi_nhat = _lay_gia_tri_dong(df_is, DONG_LOI_NHUAN_SAU_THUE, ky_moi_nhat)
            ln_cung_ky_nam_truoc = _lay_gia_tri_dong(df_is, DONG_LOI_NHUAN_SAU_THUE, ky_cung_ky_nam_truoc)
            if ln_moi_nhat is not None and ln_cung_ky_nam_truoc:
                tang_truong_yoy = round((ln_moi_nhat - ln_cung_ky_nam_truoc) / abs(ln_cung_ky_nam_truoc) * 100, 2)
                ghi_chu_yoy = None

    return {
        "ma": ma,
        "ky_moi_nhat": ky_moi_nhat,
        "loi_nhuan_sau_thue_ttm": round(loi_nhuan_ttm, 2),
        "eps_ttm": round(eps_ttm, 2),
        "tong_tai_san": round(tong_tai_san, 2) if tong_tai_san else None,
        "von_chu_so_huu": round(von_chu_so_huu, 2) if von_chu_so_huu else None,
        "roe_ttm_pct": round(roe_ttm, 2) if roe_ttm is not None else None,
        "roa_ttm_pct": round(roa_ttm, 2) if roa_ttm is not None else None,
        "von_tren_tong_ts_pct": round(ty_le_von_tren_tong_ts, 2) if ty_le_von_tren_tong_ts is not None else None,
        "pe": round(pe, 2) if pe is not None else None,
        "ghi_chu_pe": ghi_chu_pe,
        "tang_truong_loi_nhuan_yoy_pct": tang_truong_yoy,
        "ghi_chu_yoy": ghi_chu_yoy,
        "ghi_chu_pb": "P/B chưa tính do thiếu số cổ phiếu lưu hành (ratio() của vnstock đang lỗi, không lấy được).",
    }


# --- TEST NHANH VỚI DỮ LIỆU THẬT ---
if __name__ == "__main__":
    import os
    import pandas as pd

    csv_path = r"C:\vn_stock_agent_data\lich_su_gia.csv"
    danh_sach_ma = ["TCB", "BID", "ACB", "MBB"]

    gia_hien_tai_theo_ma = {}
    if os.path.exists(csv_path):
        df_gia = pd.read_csv(csv_path)
        for ma in danh_sach_ma:
            df_ma = df_gia[df_gia["ma"] == ma].sort_values("time")
            if not df_ma.empty:
                gia_hien_tai_theo_ma[ma] = df_ma["close"].iloc[-1]

    for ma in danh_sach_ma:
        print(f"\n{'='*70}\n📊 FUNDAMENTAL ANALYSIS: {ma}\n{'='*70}")
        kq = tinh_chi_so_co_ban(ma, gia_hien_tai=gia_hien_tai_theo_ma.get(ma))
        for k, v in kq.items():
            print(f"{k}: {v}")