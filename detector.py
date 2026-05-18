#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detector.py — 탈락 감지 모듈 v4
흰색 텍스트 마스킹 + EasyOCR 방식
- 고정 ROI에서 배경 제거 후 흰색 글자만 추출
- EasyOCR로 "탈락" 텍스트 직접 인식
- 배경 색상 완전 무관, 정확도 최고
"""

import cv2
import numpy as np
from pathlib import Path

# ── 탈락 텍스트 고정 ROI ──
ROI_X1 = 0.28
ROI_X2 = 0.58
ROI_Y1 = 0.62
ROI_Y2 = 0.78

# 흰색 텍스트 HSV 범위
WHITE_LOWER = np.array([0,   0, 170])
WHITE_UPPER = np.array([180, 45, 255])

KEYWORD = "탈락"

_ocr_reader = None


def init_ocr() -> bool:
    """EasyOCR 초기화 (최초 1회)"""
    global _ocr_reader
    if _ocr_reader is not None:
        return True
    try:
        import easyocr
        print("[DETECTOR] EasyOCR 초기화 중...")
        _ocr_reader = easyocr.Reader(["ko", "en"], gpu=False)
        print("[DETECTOR] EasyOCR 초기화 완료")
        print(f"[DETECTOR] 고정 ROI: x={ROI_X1*100:.0f}~{ROI_X2*100:.0f}%, "
              f"y={ROI_Y1*100:.0f}~{ROI_Y2*100:.0f}%")
        return True
    except ImportError:
        print("[DETECTOR] easyocr 미설치 → pip install easyocr")
        return False
    except Exception as e:
        print(f"[DETECTOR] EasyOCR 초기화 실패: {e}")
        return False


# 하위 호환성 유지
def load_template() -> bool:
    return init_ocr()


def extract_roi(frame) -> np.ndarray:
    """탈락 텍스트 고정 위치 ROI 추출"""
    h, w = frame.shape[:2]
    x1 = int(w * ROI_X1)
    x2 = int(w * ROI_X2)
    y1 = int(h * ROI_Y1)
    y2 = int(h * ROI_Y2)
    return frame[y1:y2, x1:x2]


def mask_white_text(roi) -> np.ndarray:
    """
    배경 제거 후 흰색 텍스트만 추출
    HSV 색공간에서 흰색 범위 마스킹
    """
    hsv  = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, WHITE_LOWER, WHITE_UPPER)

    # 노이즈 제거
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    mask   = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask   = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    # 흰색 텍스트만 남긴 이미지 (배경 검정)
    result = cv2.bitwise_and(roi, roi, mask=mask)
    return result, mask


def preprocess_for_ocr(roi) -> np.ndarray:
    """
    OCR 전처리
    1. 흰색 텍스트 마스킹
    2. 그레이스케일 변환
    3. 업스케일 (3x)
    4. 이진화
    """
    # 흰색 마스킹
    white_img, mask = mask_white_text(roi)

    # 그레이스케일
    gray = cv2.cvtColor(white_img, cv2.COLOR_BGR2GRAY)

    # 업스케일 3x
    gray = cv2.resize(gray, None, fx=3, fy=3,
                      interpolation=cv2.INTER_CUBIC)

    # 이진화
    _, binary = cv2.threshold(gray, 30, 255, cv2.THRESH_BINARY)

    return binary


def has_enough_white(roi, min_ratio: float = 0.008) -> bool:
    """
    흰색 픽셀 최소 비율 체크 (빠른 1차 필터)
    탈락 텍스트가 없으면 흰색 픽셀이 거의 없음
    """
    hsv  = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, WHITE_LOWER, WHITE_UPPER)
    return (np.count_nonzero(mask) / mask.size) >= min_ratio


def detect(frame, threshold: float = 0.75) -> tuple:
    """
    흰색 텍스트 마스킹 + EasyOCR 감지
    반환: (감지여부, 점수)
    - 점수: 1.0 = 정확히 "탈락" 감지, 0.0 = 미감지
    """
    if _ocr_reader is None:
        init_ocr()
    if _ocr_reader is None:
        return False, 0.0

    try:
        roi = extract_roi(frame)

        # 1차 필터: 흰색 픽셀 충분한지 확인 (빠름)
        if not has_enough_white(roi):
            return False, 0.0

        # 2차: EasyOCR 실행 (흰색 마스킹 전처리 후)
        processed = preprocess_for_ocr(roi)

        results = _ocr_reader.readtext(processed, detail=0)
        text    = " ".join(results)

        if KEYWORD in text:
            return True, 1.0

        # "탈" 또는 "락" 단독 감지 시 부분 점수
        if "탈" in text or "락" in text:
            return False, 0.5

        return False, 0.0

    except Exception as e:
        print(f"[DETECTOR] 오류: {e}")
        return False, 0.0


def visualize_roi(frame) -> np.ndarray:
    """디버그용 — ROI + 흰색 마스크 시각화"""
    vis = frame.copy()
    h, w = vis.shape[:2]
    x1 = int(w * ROI_X1)
    x2 = int(w * ROI_X2)
    y1 = int(h * ROI_Y1)
    y2 = int(h * ROI_Y2)
    cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 255, 0), 3)

    # 흰색 마스크 오버레이
    roi = frame[y1:y2, x1:x2]
    _, mask = mask_white_text(roi)
    overlay = vis[y1:y2, x1:x2].copy()
    overlay[mask > 0] = [0, 255, 255]  # 흰색 픽셀을 노란색으로 표시
    vis[y1:y2, x1:x2] = cv2.addWeighted(vis[y1:y2, x1:x2], 0.6, overlay, 0.4, 0)

    return vis
