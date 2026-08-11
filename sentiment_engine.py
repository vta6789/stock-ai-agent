"""
sentiment_engine.py
===================
Module 4b: SENTIMENT ANALYSIS ENGINE

Chức năng:
1. Scrape tin tức doanh nghiệp từ CafeF theo URL pattern:
   https://cafef.vn/du-lieu/tin-doanh-nghiep/{ma}/Event.chn
2. Dùng Gemini (gemini-3.6-flash) phân tích tâm lý thị trường (BUY/SELL/HOLD bias, Tích cực/Tiêu cực/Trung lập).
3. Đóng gói kết quả đầu ra chuẩn Pydantic Schema.

NGUYÊN TẮC: File độc lập hoàn toàn, không chỉnh sửa vào bất kỳ file code cũ nào.
"""

import os
import sys
import json
import logging
import requests
import time
from bs4 import BeautifulSoup
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Thư viện Gemini SDK mới nhất theo chuẩn dự án
from google import genai
from google.genai import types as genai_types

sys.stdout.reconfigure(encoding='utf-8')

# Cấu hình Logging
logger = logging.getLogger("sentiment_engine")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False   # <-- THÊM DÒNG NÀY: chặn log truyền lên root logger,
                                 #     tránh bị in trùng 2 lần khi main.py cũng có handler riêng
# Nạp biến môi trường
load_dotenv()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")   
MODEL_NAME = "gemini-3.5-flash-lite"


# ==================== PYDANTIC SCHEMA ====================

class SentimentAnalysisOutput(BaseModel):
    nhan_dinh: str = Field(description="Đánh giá tổng quan: Tích cực, Tiêu cực, hoặc Trung lập")
    diem_tam_ly: int = Field(description="Thang điểm tâm lý từ -10 (Rất tiêu cực) đến +10 (Rất tích cực)")
    tom_tat_tin_tuc: str = Field(description="Tóm tắt nội dung chính từ các bản tin ngắn gọn trong 2-3 câu")
    tac_dong_ngan_han: str = Field(description="Dự báo tác động ngắn hạn tới giá cổ phiếu")


# ==================== SCRAPER LAYER (CAFEF) ====================

def cao_tin_tuc_cafef(ma: str, so_luong_tin: int = 5) -> list:
    """
    Cào danh sách tin tức mới nhất của 1 mã cổ phiếu từ CafeF.
    URL Pattern: https://cafef.vn/du-lieu/tin-doanh-nghiep/{ma}/Event.chn
    """
    url = f"https://cafef.vn/du-lieu/tin-doanh-nghiep/{ma.upper()}/Event.chn"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"[{ma}] Không thể truy cập CafeF (Status code: {response.status_code})")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        danh_sach_tin = []

        # Bóc tách cấu trúc tin tức CafeF (bổ sung nhiều selector dự phòng DOM thay đổi)
        items = soup.select("#divEvents li")  # DA SUA: dung dung DOM that cua CafeF (verify 08/2026)
        
        for item in items:
            tieu_de_elem = item.select_one("a.docnhanhTitle")  # DA SUA: class that trong DOM
            thoi_gian_elem = item.select_one(".timeTitle")  # DA SUA: class that trong DOM

            tieu_de = tieu_de_elem.get_text(strip=True) if tieu_de_elem else ""
            thoi_gian = thoi_gian_elem.get_text(strip=True) if thoi_gian_elem else ""

            if tieu_de:
                format_tin = f"[{thoi_gian}] {tieu_de}" if thoi_gian else tieu_de
                danh_sach_tin.append(format_tin)

            if len(danh_sach_tin) >= so_luong_tin:
                break

        # Fallback nếu cấu trúc DOM khác
        if not danh_sach_tin:
            all_links = soup.select("a[title]")
            for link in all_links:
                title = link.get("title", "").strip()
                if len(title) > 15 and title not in danh_sach_tin:
                    danh_sach_tin.append(title)
                if len(danh_sach_tin) >= so_luong_tin:
                    break

        return danh_sach_tin

    except Exception as e:
        logger.error(f"[{ma}] Lỗi ngoại lệ khi cào dữ liệu CafeF: {e}")
        return []


# ==================== LLM ENGINE (GEMINI 3.6 FLASH) ====================

def phan_tich_tam_ly_bang_gemini(ma: str, danh_sach_tin: list, so_lan_thu: int = 0) -> Optional[SentimentAnalysisOutput]:
    """
    Gọi Gemini API (lần gọi thứ 2 trong hệ thống) để phân tích tâm lý từ tin tức.
    """
    if not GEMINI_API_KEY:
        logger.error("Không tìm thấy GEMINI_API_KEY trong môi trường.")
        return None

    if not danh_sach_tin:
        return SentimentAnalysisOutput(
            nhan_dinh="Trung lập",
            diem_tam_ly=0,
            tom_tat_tin_tuc="Không tìm thấy tin tức mới nào từ CafeF.",
            tac_dong_ngan_han="Chưa có thông tin tác động."
        )

    client = genai.Client(api_key=GEMINI_API_KEY)
    text_tin_tuc = "\n".join([f"- {tin}" for tin in danh_sach_tin])

    prompt_system = """
    Bạn là chuyên gia Phân tích Tâm lý Thị trường (Market Sentiment Analyst) tại Việt Nam.
    Nhiệm vụ: Đọc các tiêu đề/tin tức cào từ CafeF về 1 mã cổ phiếu, sau đó đánh giá tâm lý nhà đầu tư và thị trường.
    Yêu cầu:
    - Trả về ĐÚNG cấu trúc JSON Schema được yêu cầu.
    - Đánh giá khách quan, không tự bịa thông tin không có trong bản tin.
    """

    prompt_user = f"""
    Mã cổ phiếu: {ma}
    Danh sách tin tức mới nhất từ CafeF:
    {text_tin_tuc}

    Hãy phân tích tâm lý thị trường cho mã {ma} dựa trên các thông tin trên.
    """

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt_user,
            config=genai_types.GenerateContentConfig(
                system_instruction=prompt_system,
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=SentimentAnalysisOutput,
            ),
        )
        data = json.loads(response.text)
        return SentimentAnalysisOutput(**data)

    except Exception as e:
        msg = str(e)
        la_loi_tam_thoi = "429" in msg or "503" in msg or "RESOURCE_EXHAUSTED" in msg or "UNAVAILABLE" in msg
        if la_loi_tam_thoi and so_lan_thu < 2:
            thoi_gian_cho = 15 * (so_lan_thu + 1)
            logger.warning(f"[{ma}] Gemini qua tai tam thoi (lan {so_lan_thu + 1}/2) - cho {thoi_gian_cho}s roi thu lai...")
            time.sleep(thoi_gian_cho)
            return phan_tich_tam_ly_bang_gemini(ma, danh_sach_tin, so_lan_thu + 1)
        logger.error(f"[{ma}] Lỗi khi gọi Gemini phân tích tâm lý: {e}")
        return None


# ==================== HÀM INTERFACE CHÍNH ====================

def phan_tich_sentiment_day_du(ma: str) -> dict:
    """
    Hàm giao tiếp chính với bên ngoài:
    Cào tin -> Gọi Gemini -> Trả về dictionary để nhúng thẳng vào hệ thống.
    """
    logger.info(f"🔍 [{ma}] Bắt đầu cào tin tức CafeF...")
    ds_tin = cao_tin_tuc_cafef(ma, so_luong_tin=5)
    
    logger.info(f"📰 [{ma}] Lấy được {len(ds_tin)} tin tức. Đang phân tích qua Gemini...")
    kq_sentiment = phan_tich_tam_ly_bang_gemini(ma, ds_tin)

    if kq_sentiment:
        return {
            "trang_thai": "THANH_CONG",
            "so_luong_tin": len(ds_tin),
            "nhan_dinh": kq_sentiment.nhan_dinh,
            "diem_tam_ly": kq_sentiment.diem_tam_ly,
            "tom_tat": kq_sentiment.tom_tat_tin_tuc,
            "tac_dong": kq_sentiment.tac_dong_ngan_han,
            "chi_tiet_text": (
                f"Tâm lý: {kq_sentiment.nhan_dinh} (Điểm: {kq_sentiment.diem_tam_ly}/10). "
                f"Tóm tắt: {kq_sentiment.tom_tat_tin_tuc}"
            )
        }
    
    return {
        "trang_thai": "THAT_BAI",
        "so_luong_tin": 0,
        "nhan_dinh": "Chưa có dữ liệu",
        "chi_tiet_text": "Không thể lấy dữ liệu phân tích tâm lý."
    }


# ==================== RUN TEST ĐỘC LẬP ====================

if __name__ == "__main__":
    ma_test = "TCB"
    print(f"\n=================== TEST SENTIMENT ENGINE: {ma_test} ===================")
    ket_qua = phan_tich_sentiment_day_du(ma_test)
    print(json.dumps(ket_qua, indent=2, ensure_ascii=False))