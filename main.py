"""
main.py
========
FILE TRUNG TÂM: chạy tuần tự toàn bộ pipeline (TA -> Risk -> FA -> Sentiment -> Brain)
cho từng mã trong watchlist, lưu kết quả + lịch sử quyết định.

QUAN TRỌNG: file này KHÔNG tự fetch giá mới - data_engine.py đã chạy 24/7 riêng
lo việc đó rồi. main.py chỉ ĐỌC CSV giá đã có sẵn, tránh gọi trùng lặp API vnstock
(và tránh cả 2 tiến trình cùng ghi vào 1 file CSV gây xung đột).

RATE LIMIT GEMINI: mỗi mã tốn 2 lần gọi Gemini (Sentiment Engine + Agent Brain).
Gói free tier chỉ cho ~10-15 requests/phút, nên có nghỉ giữa các mã để an toàn.

LƯU KẾT QUẢ: ghi ra file Excel (.xlsx) 2 sheet thay vì CSV phẳng - "Tổng quan"
(số liệu chính, dễ nhìn tổng thể) và "Phân tích chi tiết" (4 đoạn giải thích dài,
tách riêng để sheet Tổng quan không bị rối mắt).

GHI FILE AN TOÀN (atomic write): wb.save() ghi trực tiếp đè lên file thật - nếu
tiến trình bị crash/kill giữa chừng, file gốc sẽ bị hỏng (0 byte / không đọc được).
Để tránh việc này, ta ghi ra file tạm .tmp trước, xong mới os.replace() đổi tên đè
lên file thật - thao tác đổi tên là atomic ở cấp hệ điều hành nên không bao giờ để
lại file nửa vời.
"""

import os
import sys
import time
import logging
from datetime import datetime

import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_brain
import bot_telegram


# ==================== CẤU HÌNH ====================
CSV_GIA_PATH = r"C:\vn_stock_agent_data\lich_su_gia.csv"
XLSX_PATH = r"C:\vn_stock_agent_data\phan_tich_ky_thuat.xlsx"
XLSX_TMP_PATH = r"C:\vn_stock_agent_data\phan_tich_ky_thuat.xlsx.tmp"
LOG_FOLDER = r"C:\vn_stock_agent_data\logs"
LOG_PATH = os.path.join(LOG_FOLDER, "main.log")

WATCHLIST = [
    "VCB", "BID", "TCB", "VPB", "MBB", "ACB",
    "HDB", "VRE", "OCB", "LPB", "HPG", "FPT",
]

# Giãn cách giữa các mã (giây) - né rate limit Gemini free tier (~10-15 req/phút,
# mỗi mã tốn 2 request). 10s/mã => tối đa 6 mã/phút, an toàn dưới ngưỡng.
KHOANG_NGHI_GIUA_MA_GIAY = 10

# ---- Định dạng Excel ----
COT_TONG_QUAN = ["Mã", "Trạng thái", "Độ tin cậy", "Giá vào", "Cắt lỗ", "Chốt lời", "Mức độ rủi ro", "Thời gian phân tích", "Nhận định & lời khuyên"]
DO_RONG_TONG_QUAN = [5, 20, 20, 10, 10, 10, 14, 20, 60]

COT_PHAN_TICH = ["Mã", "Thời gian phân tích", "Phân tích kỹ thuật", "Phân tích cơ bản", "Phân tích tâm lý", "Phân tích dòng tiền"]
DO_RONG_PHAN_TICH = [5, 20, 55, 55, 55, 55]

MAU_HEADER = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
FONT_HEADER = Font(bold=True, color="FFFFFF")
MAU_BUY = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
MAU_SELL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
# ====================================================


os.makedirs(LOG_FOLDER, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


def _tao_sheet_moi(wb, ten_sheet: str, cot: list, do_rong: list):
    """Tạo 1 sheet mới với header đã định dạng (in đậm, nền xanh đậm, chữ trắng), freeze hàng đầu."""
    ws = wb.create_sheet(ten_sheet)
    ws.append(cot)
    for idx, c in enumerate(cot, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.font = FONT_HEADER
        cell.fill = MAU_HEADER
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(idx)].width = do_rong[idx - 1]
    ws.freeze_panes = "A2"
    return ws


def _mo_hoac_tao_workbook():
    """Mở file Excel đã có, hoặc tạo mới với đúng 2 sheet + định dạng nếu chưa tồn tại.
    Nếu file tồn tại nhưng bị hỏng (VD 0 byte do crash lần trước), tự log cảnh báo
    và tạo workbook mới thay vì để lỗi làm chết cả pipeline."""
    if os.path.exists(XLSX_PATH):
        try:
            return openpyxl.load_workbook(XLSX_PATH)
        except Exception as e:
            logger.warning(f"File Excel hiện tại bị hỏng, không đọc được ({e}) - tạo file mới thay thế.")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # xóa sheet trắng mặc định
    _tao_sheet_moi(wb, "Tổng quan", COT_TONG_QUAN, DO_RONG_TONG_QUAN)
    _tao_sheet_moi(wb, "Phân tích chi tiết", COT_PHAN_TICH, DO_RONG_PHAN_TICH)
    return wb


def luu_toan_bo_phien_len_dau(danh_sach_ket_qua: list) -> bool:
    """
    Ghi 1 kết quả vào file Excel 2 sheet - "Tổng quan" và "Phân tích chi tiết",
    nối với nhau qua cặp (Mã, Thời gian phân tích). Tô màu xanh cho BUY, đỏ cho
    SELL ở sheet Tổng quan để dễ quét mắt. Tự retry nếu bị PermissionError
    (OneDrive/Excel đang mở khóa file).

    GHI AN TOÀN (atomic): lưu ra file .tmp trước, chỉ os.replace() đổi tên đè lên
    file thật SAU KHI đã ghi xong hoàn toàn - nếu crash giữa chừng, file .tmp bị
    hỏng nhưng file .xlsx thật vẫn nguyên vẹn từ lần lưu trước.
    """
    ket_qua_hop_le = [(qd, ma) for qd, ma in danh_sach_ket_qua if qd is not None]
    if not ket_qua_hop_le:
        logger.warning("Không có kết quả hợp lệ nào để lưu vào Excel.")
        return False

    so_dong = len(ket_qua_hop_le)
    thoi_diem = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for attempt in range(5):
        try:
            wb = _mo_hoac_tao_workbook()
            ws_tong_quan = wb["Tổng quan"]
            ws_phan_tich = wb["Phân tích chi tiết"]

            ws_tong_quan.insert_rows(2, amount=so_dong)
            ws_phan_tich.insert_rows(2, amount=so_dong)
            for i_dong, (quyet_dinh, ma) in enumerate(ket_qua_hop_le):
                dong_moi = 2 + i_dong
                analysis = quyet_dinh.analysis_summary
                gia_tri = [
                    ma, quyet_dinh.action, quyet_dinh.confidence_score,
                    quyet_dinh.entry_price, quyet_dinh.stop_loss, quyet_dinh.target_price,
                    quyet_dinh.risk_level, thoi_diem, quyet_dinh.nhan_dinh_tong_quan
                ]
                for idx, v in enumerate(gia_tri, start=1):
                    ws_tong_quan.cell(row=dong_moi, column=idx, value=v)

                ws_tong_quan.cell(row=dong_moi, column=9).alignment = Alignment(wrap_text=True, vertical="top")

                if quyet_dinh.action == "BUY":
                    ws_tong_quan.cell(row=dong_moi, column=2).fill = MAU_BUY
                elif quyet_dinh.action == "SELL":
                    ws_tong_quan.cell(row=dong_moi, column=2).fill = MAU_SELL

                gia_tri_ct = [ma, thoi_diem, analysis.technical, analysis.fundamental, analysis.sentiment, analysis.money_flow]
                for idx, v in enumerate(gia_tri_ct, start=1):
                    ws_phan_tich.cell(row=dong_moi, column=idx, value=v)
                for col in range(3, 7):
                    ws_phan_tich.cell(row=dong_moi, column=col).alignment = Alignment(wrap_text=True, vertical="top")

            # Ghi ra file tạm trước, không đụng tới file thật.
            wb.save(XLSX_TMP_PATH)
            # Chỉ khi ghi .tmp thành công 100% mới đổi tên đè lên file thật (atomic).
            os.replace(XLSX_TMP_PATH, XLSX_PATH)
            logger.info(f"Đã lưu {so_dong} kết quả vào {XLSX_PATH}")
            return True
        except PermissionError:
            logger.warning(f"File Excel đang bị khóa (OneDrive/Excel đang mở?) - chờ 8s rồi thử lại (lần {attempt + 1}/5)...")
            time.sleep(8)
        except Exception as e:
            logger.error(f"Lỗi không mong đợi khi lưu Excel (lần {attempt + 1}/5): {e}")
            time.sleep(8)
        finally:
            # Dọn file .tmp nếu còn sót lại do lỗi giữa chừng.
            if os.path.exists(XLSX_TMP_PATH):
                try:
                    os.remove(XLSX_TMP_PATH)
                except OSError:
                    pass

    logger.error(f"Không ghi được vào file Excel sau 5 lần thử ({so_dong} kết quả bị mất).")
    return False


def chay_toan_bo_watchlist(danh_sach_ma: list = None) -> list:
    """
    Chạy pipeline đầy đủ cho từng mã trong danh_sach_ma (mặc định = WATCHLIST),
    tuần tự (không song song, để không vượt rate limit Gemini).
    Trả về list các QuyetDinhGiaoDich hợp lệ đã lấy được.
    """
    danh_sach_ma = danh_sach_ma or WATCHLIST

    if not os.path.exists(CSV_GIA_PATH):
        logger.error(f"Không tìm thấy dữ liệu giá tại {CSV_GIA_PATH} - hãy chạy data_engine.py trước để có dữ liệu.")
        return []

    df_all = pd.read_csv(CSV_GIA_PATH)
    df_vnindex = df_all[df_all["ma"] == "VNINDEX"].sort_values("time").reset_index(drop=True)

    ket_qua_toan_bo = []
    ket_qua_de_luu = []
    logger.info("=" * 60)
    logger.info(f"BẮT ĐẦU PHIÊN PHÂN TÍCH - {len(danh_sach_ma)} mã")
    logger.info("=" * 60)

    for i, ma in enumerate(danh_sach_ma, start=1):
        logger.info(f"[{i}/{len(danh_sach_ma)}] Đang phân tích {ma}...")
        try:
            if ma not in df_all["ma"].unique():
                logger.warning(f"[{ma}] Chưa có dữ liệu giá trong CSV, bỏ qua.")
                continue

            quyet_dinh = agent_brain.phan_tich_va_quyet_dinh(ma, df_all, df_vnindex)

            if quyet_dinh:
                logger.info(f"[{ma}] => {quyet_dinh.action} (confidence_score={quyet_dinh.confidence_score})")
                ket_qua_toan_bo.append(quyet_dinh)
                ket_qua_de_luu.append((quyet_dinh, ma))
                bot_telegram.gui_canh_bao_neu_can(quyet_dinh, ma)
            else:
                logger.warning(f"[{ma}] Không lấy được quyết định hợp lệ (agent_brain trả về None).")

        except Exception as e:
            # Bắt MỌI lỗi ở cấp độ từng mã - 1 mã lỗi không được làm dừng cả phiên
            logger.error(f"[{ma}] Lỗi không xác định, bỏ qua mã này: {e}")
        finally:
            time.sleep(KHOANG_NGHI_GIUA_MA_GIAY)

    # QUAN TRỌNG: lưu toàn bộ kết quả của phiên vào Excel - trước đây bước này
    # bị thiếu, khiến bot Telegram vẫn push cảnh báo bình thường nhưng file
    # phan_tich_ky_thuat.xlsx không hề được cập nhật.
    if ket_qua_de_luu:
        luu_toan_bo_phien_len_dau(ket_qua_de_luu)
    else:
        logger.warning("Không có kết quả nào trong phiên này để lưu vào Excel.")

    logger.info("=" * 60)
    logger.info(f"HOÀN TẤT PHIÊN PHÂN TÍCH: {len(ket_qua_toan_bo)}/{len(danh_sach_ma)} mã có quyết định hợp lệ.")
    logger.info(f"Kết quả lưu tại: {XLSX_PATH}")
    logger.info("=" * 60)

    return ket_qua_toan_bo


if __name__ == "__main__":
    chay_toan_bo_watchlist()
