"""
agent_brain.py
================
Module 6: CORE AGENT BRAIN

Tổng hợp output từ TA Engine + Risk Engine (và FA/Sentiment sau này khi có)
-> gọi Google Gemini để đưa ra quyết định cuối cùng (BUY/SELL/HOLD)
-> validate output bằng pydantic (dùng structured output/response_schema
   của Gemini để giảm tối đa lỗi JSON sai định dạng), tự retry nếu cần.

COST ROUTING: đây là 1 trong 2 nơi DUY NHẤT gọi API LLM trong toàn hệ thống
(nơi còn lại là Sentiment Engine - Module 4, chưa build). Mọi module khác
(Data/TA/Risk/FA) xử lý 100% bằng code Python thuần, KHÔNG gọi LLM.

DÙNG GEMINI (không dùng Anthropic API - theo yêu cầu dự án): SDK chính thức
mới nhất `google-genai` (khác gói cũ đã deprecated `google-generativeai`).
Cài đặt: pip install google-genai
API key: lấy tại https://aistudio.google.com/apikey (có free tier), đọc từ
biến môi trường GEMINI_API_KEY trong file .env.

Model: gemini-3.6-flash. LƯU Ý LỊCH SỬ (để hiểu tại sao đổi qua đổi lại):
1) Ban đầu chọn gemini-2.5-pro -> lỗi 429, quota free tier = 0 (Pro đã bị đưa
   hẳn ra khỏi free tier từ đầu 2026, cần bật billing mới dùng được).
2) Đổi tạm sang gemini-2.5-flash -> lỗi 404 "no longer available to new users"
   (Google đã ngừng cấp 2.5-flash cho project mới, thay bằng dòng Gemini 3).
3) Chốt dùng gemini-3.6-flash (GA, bản Flash mới nhất tại 08/2026) - có quota
   free tier thật (~10-15 RPM, ~1500 RPD tại thời điểm viết, không cần thẻ).
Nếu tương lai lại gặp lỗi tương tự (Google đổi model rất nhanh), kiểm tra
model mới nhất tại https://ai.google.dev/gemini-api/docs/models rồi cập nhật
MODEL_NAME - đây là điểm thay đổi DUY NHẤT cần sửa, không đụng gì khác.
"""

import os
import json
import logging
import  time
from datetime import datetime
from typing import Literal, Optional

import pandas as pd

from dotenv import load_dotenv
load_dotenv()  # đọc file .env cùng thư mục (nếu có) -> tránh phụ thuộc biến môi trường
                # của từng terminal session riêng lẻ, ổn định dù chạy từ VS Code,
                # PowerShell, hay tự động hoá (cron/task scheduler) sau này.

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("agent_brain")

SO_LAN_RETRY_TOI_DA = 2
MODEL_NAME = "gemini-3.5-flash-lite"


# ==================== PYDANTIC SCHEMA ====================

class AnalysisSummary(BaseModel):
    technical: str = Field(description="Nhận định dựa trên chỉ báo kỹ thuật (RSI/MACD/EMA/Volume)")
    fundamental: str = Field(description="Nhận định cơ bản dựa trên ROE/ROA/P-E/tăng trưởng LN (nếu có dữ liệu FA)")
    sentiment: str = Field(description="Nhận định tâm lý thị trường dựa trên tin tức (nếu có)")
    money_flow: str = Field(description="Nhận định dòng tiền dựa trên volume/volume_spike")


class QuyetDinhGiaoDich(BaseModel):
    ticker: str
    action: Literal["BUY", "SELL", "HOLD"]
    confidence_score: float = Field(ge=0, le=100)
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    risk_level: Literal["THAP", "TRUNG_BINH", "CAO"]
    nhan_dinh_tong_quan: str = Field(description="Nhận định tổng quan ngắn gọn (2-4 câu, giọng chuyên gia) kèm lời khuyên hành động rõ ràng cho nhà đầu tư, tổng hợp toàn bộ TA+Risk+FA+Sentiment thành 1 đoạn dễ đọc")
    analysis_summary: AnalysisSummary
# ===========================================================


def _dung_du_lieu_ta_risk_thanh_prompt(ma: str, ta_output: dict, risk_output: dict, fa_output: dict = None, sentiment_output: dict = None) -> str:
    
    # 1. Xử lý Dữ liệu Dòng tiền (Money Flow)
    phan_money_flow = "Không có thông tin khối lượng."
    if ta_output.get("df") is not None and not ta_output["df"].empty and not pd.isna(ta_output["df"].iloc[-1].get("volume")):
        dong_cuoi_mf = ta_output["df"].iloc[-1]
        vol_mf = dong_cuoi_mf.get("volume")
        vol_ma20_mf = dong_cuoi_mf.get("volume_ma20")
        vol_spike_mf = dong_cuoi_mf.get("volume_spike")
        phan_money_flow = f"Khối lượng phiên gần nhất: {vol_mf:,.0f}"
        if vol_ma20_mf is not None and not pd.isna(vol_ma20_mf):
            phan_money_flow += f" | TB20: {vol_ma20_mf:,.0f}"
        phan_money_flow += f" | Đột biến: {'CÓ' if vol_spike_mf else 'KHÔNG'}"

    # 2. Xử lý Dữ liệu Rủi ro (Risk)
    phan_risk = "Chưa có dữ liệu rủi ro."
    if risk_output and not risk_output.get("loi"):
        phan_risk = f"""
- Entry đề xuất: {risk_output.get('entry')}
- R:R ratio: {risk_output.get('risk_reward_ratio')} | Beta: {risk_output.get('beta')} | Max Drawdown: {risk_output.get('max_drawdown_pct')}%
- Stop Loss: {risk_output.get('stop_loss')}
- Take Profit: {risk_output.get('take_profit')}
""".strip()

    # 3. Xử lý Dữ liệu Cơ bản (FA)
    phan_fa = "Chưa có dữ liệu Fundamental Analysis."
    if fa_output and not fa_output.get("loi"):
        phan_fa = f"""
- ROE (TTM): {fa_output.get('roe_ttm_pct')}% | ROA (TTM): {fa_output.get('roa_ttm_pct')}%
- P/E: {fa_output.get('pe')} {f"({fa_output.get('ghi_chu_pe')})" if fa_output.get('ghi_chu_pe') else ""}
- Tăng trưởng lợi nhuận YoY: {fa_output.get('tang_truong_loi_nhuan_yoy_pct')}% {f"({fa_output.get('ghi_chu_yoy')})" if fa_output.get('ghi_chu_yoy') else ""}
- Vốn CSH/Tổng tài sản: {fa_output.get('von_tren_tong_ts_pct')}%
""".strip()

    # 4. Xử lý Dữ liệu Tâm lý (Sentiment)
    phan_sentiment = "Chưa có dữ liệu Sentiment."
    if sentiment_output and sentiment_output.get("trang_thai") == "THANH_CONG":
        phan_sentiment = f"""
- Nhận định: {sentiment_output.get('nhan_dinh')}
- Điểm tâm lý: {sentiment_output.get('diem_tam_ly')}/10
- Tóm tắt: {sentiment_output.get('tom_tat')}
- Tác động ngắn hạn: {sentiment_output.get('tac_dong')}
""".strip()

    # 5. Lắp ráp Prompt cuối cùng
    return f"""
Dữ liệu Technical Analysis (mã {ma}):
- Xu hướng: {ta_output.get('xu_huong')}
- Tín hiệu multi-factor gần nhất: {ta_output.get('tin_hieu_gan_nhat')}
- Hỗ trợ gần nhất: {ta_output.get('ho_tro')}
- Kháng cự gần nhất: {ta_output.get('khang_cu')}

Dữ liệu Dòng tiền (Money Flow):
- {phan_money_flow}

Dữ liệu Risk Management:
{phan_risk}

Dữ liệu Fundamental Analysis:
{phan_fa}

Dữ liệu Sentiment Analysis:
{phan_sentiment}
""".strip()


def _system_prompt() -> str:
    return """
Bạn là Trưởng phòng Phân tích Đầu tư tại 1 công ty chứng khoán Việt Nam.
Nhiệm vụ: tổng hợp dữ liệu Technical Analysis + Risk Management + Fundamental
Analysis (nếu có) để đưa ra quyết định giao dịch cuối cùng. Nếu 1 mục nào đó
ghi "Chưa có dữ liệu", hãy ghi đúng như vậy trong analysis_summary tương ứng,
KHÔNG được tự bịa số liệu.
Nếu Risk Management ghi "KHÔNG tính điểm giao dịch", action PHẢI là HOLD và
entry_price/stop_loss/target_price để trống (null).
Với trường "nhan_dinh_tong_quan": viết 2-4 câu, giọng văn tự nhiên như lời
khuyên trực tiếp của chuyên gia, tổng hợp NGẮN GỌN các yếu tố quan trọng nhất
thành 1 nhận định + khuyến nghị hành động rõ ràng - đây là phần người đọc sẽ
đọc ĐẦU TIÊN để nắm nhanh "nên làm gì" mà không cần đọc hết 4 mục phân tích
chi tiết bên dưới.
Trả lời ĐÚNG theo schema JSON đã được cấu hình, không thêm giải thích ngoài JSON.
""".strip()


def tong_hop_quyet_dinh(ma: str, ta_output: dict, risk_output: dict, fa_output: dict = None, sentiment_output: dict = None) -> Optional[QuyetDinhGiaoDich]:
    """
    Gọi Gemini tổng hợp TA + Risk (+ FA nếu có) -> quyết định cuối cùng.
    Dùng response_schema (structured output) của Gemini để Gemini BẮT BUỘC trả
    đúng JSON schema ngay từ đầu - giảm hẳn tỷ lệ lỗi so với chỉ yêu cầu qua prompt.
    Vẫn giữ retry tối đa SO_LAN_RETRY_TOI_DA lần cho các lỗi hiếm gặp (network, parse).
    Trả về None nếu hết số lần retry vẫn lỗi (không làm crash hệ thống).
    """
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    # FIX LỖI Ở ĐÂY: Phải truyền sentiment_output xuống hàm tạo prompt
    du_lieu = _dung_du_lieu_ta_risk_thanh_prompt(ma, ta_output, risk_output, fa_output, sentiment_output)
    loi_lan_truoc = ""

    for lan_thu in range(SO_LAN_RETRY_TOI_DA + 1):
        user_message = du_lieu
        if loi_lan_truoc:
            user_message += f"\n\nLẦN TRƯỚC LỖI: {loi_lan_truoc}\nHãy trả lại ĐÚNG schema JSON đã yêu cầu."

        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=user_message,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_system_prompt(),
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=QuyetDinhGiaoDich,  # ép Gemini trả đúng schema pydantic này
                ),
            )
            data = json.loads(response.text)
            ket_qua = QuyetDinhGiaoDich(**data)
            return ket_qua

        except (json.JSONDecodeError, ValidationError) as e:
            loi_lan_truoc = str(e)
            logger.warning(f"[{ma}] ... trả JSON sai định dạng (lần {lan_thu + 1}/{SO_LAN_RETRY_TOI_DA + 1}): {e}")
            continue
        except Exception as e:
            msg = str(e)
            la_loi_tam_thoi = "503" in msg or "429" in msg or "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg
            if la_loi_tam_thoi and lan_thu < SO_LAN_RETRY_TOI_DA:
                # Loi tam thoi tu phia Google (qua tai/rate limit) - cho nghi roi thu lai,
                # KHONG phai loi cua minh, khac voi loi JSON sai dinh dang o tren.
                thoi_gian_cho = 15 * (lan_thu + 1)  # 15s, 30s... tang dan moi lan retry
                logger.warning(f"[{ma}] Gemini qua tai tam thoi ({msg[:80]}) - cho {thoi_gian_cho}s roi thu lai (lan {lan_thu + 1}/{SO_LAN_RETRY_TOI_DA})...")
                time.sleep(thoi_gian_cho)
                loi_lan_truoc = ""  # khong can nhet loi cu vao prompt vi day khong phai loi noi dung
                continue
            logger.error(f"[{ma}] Lỗi khi gọi Gemini API: {e}")
            return None

    logger.error(f"[{ma}] Hết {SO_LAN_RETRY_TOI_DA} lần retry vẫn không lấy được JSON hợp lệ.")
    return None


def phan_tich_va_quyet_dinh(ma: str, df_all, df_vnindex=None) -> Optional[QuyetDinhGiaoDich]:
    logger.info(f"\n[{ma}] --- BẮT ĐẦU PHÂN TÍCH TOÀN DIỆN ---")
    
    try:
        import ta_engine as ta
        kq_ta = ta.phan_tich_ky_thuat_toan_dien(ma, df_all, df_vnindex)
    except Exception as e:
        logger.error(f"[{ma}] Lỗi nghiêm trọng ở TA Engine: {e}")
        return None
        
    try:
        import risk_engine as re
        kq_risk = re.danh_gia_rui_ro_toan_dien(ma, kq_ta)
    except Exception as e:
        logger.error(f"[{ma}] Lỗi nghiêm trọng ở Risk Engine: {e}")
        return None

    fa_output = None
    try:
        import fa_engine as fe
        gia_hien_tai = kq_ta["df"]["close"].iloc[-1] if not kq_ta["df"].empty else None
        fa_output = fe.tinh_chi_so_co_ban(ma, gia_hien_tai=gia_hien_tai)
    except Exception as e:
        logger.warning(f"[{ma}] Không lấy được dữ liệu FA: {e}")

    # GỌI SENTIMENT ENGINE (MỚI THÊM)
    sentiment_output = None
    try:
        import sentiment_engine as se
        sentiment_output = se.phan_tich_sentiment_day_du(ma)
    except Exception as e:
        logger.warning(f"[{ma}] Không lấy được dữ liệu Sentiment: {e}")

    # Gửi qua não bộ LLM để tổng hợp tất cả
    return tong_hop_quyet_dinh(ma, kq_ta, kq_risk, fa_output, sentiment_output)

# --- TEST NHANH ---
if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    import pandas as pd

    csv_path = r"C:\vn_stock_agent_data\lich_su_gia.csv"
    df_all = pd.read_csv(csv_path)
    df_vnindex = df_all[df_all["ma"] == "VNINDEX"].sort_values("time").reset_index(drop=True)

    ma = "TCB"
    print(f"🧠 Đang tổng hợp quyết định cho {ma} (bằng Gemini {MODEL_NAME})...")
    quyet_dinh = phan_tich_va_quyet_dinh(ma, df_all, df_vnindex)

    if quyet_dinh:
        print(quyet_dinh.model_dump_json(indent=2))
    else:
        print("❌ Không lấy được quyết định hợp lệ.")