#!/usr/bin/env python3
"""
Fintel.io 스크래핑 + Google Sheets 자동 업데이트 (통합 스크립트)
- 로컬 macOS / GitHub Actions Ubuntu 양쪽 동작
"""

import os
import re
import time
import random
import shutil
import subprocess
import tempfile
import warnings
from dataclasses import dataclass
from typing import List, Optional

warnings.filterwarnings("ignore")

import undetected_chromedriver as uc
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── 환경 감지 ──────────────────────────────────────────────────────────────────
import platform
IS_CI  = os.getenv("GITHUB_ACTIONS") == "true"
IS_MAC = platform.system() == "Darwin"

# Chrome 경로 — OS 기준 (self-hosted Mac runner도 macOS이므로 동일)
if IS_MAC:
    CHROME_BIN  = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    DRIVER_PATH = (
        "/Users/gsr/Library/Application Support/"
        "undetected_chromedriver/undetected_chromedriver"
    )
else:
    CHROME_BIN  = "/usr/bin/google-chrome"
    DRIVER_PATH = None   # UC 자동 다운로드

# 서비스 계정 파일 — CI 실행 시 워크플로우가 /tmp에 기록
SERVICE_ACCOUNT_FILE = (
    "/tmp/service_account.json"
    if IS_CI else
    "/Users/gsr/Desktop/Private/Google Sheets API_fintel.json"
)

# ── 상수 ──────────────────────────────────────────────────────────────────────
TICKERS = [
    "MU", "CLS", "GEV", "IREN", "COHR", "GOOGL", "SNDK", "TSLA",
    "RKLB", "PL", "SOFI", "LITE", "TEM", "IONQ", "TRX", "LAES",
    "OKLO", "AMZN", "LUV", "SOXL", "NVDA", "MRVL", "BCS", "VRT",
]

SPREADSHEET_ID    = "1ulh1dLh2k5VBDcdMHrodY53O1Bjr0rQnUM4GnpJNdBc"
SCOPES            = ["https://www.googleapis.com/auth/spreadsheets"]
COL_INST          = "O"   # 기관 보유 비중
COL_CTB           = "P"   # CTB 대주수수료

CF_WAIT           = 8.0
PAGE_LOAD_TIMEOUT = 45
RESTART_EVERY     = 6


# ── 데이터 컨테이너 ────────────────────────────────────────────────────────────
@dataclass
class StockData:
    ticker: str
    inst_pct: str = "N/A"
    ctb: str = "N/A"


# ── Chrome 메이저 버전 감지 ────────────────────────────────────────────────────
def get_chrome_major_version() -> Optional[int]:
    try:
        out = subprocess.check_output(
            [CHROME_BIN, "--version"], stderr=subprocess.DEVNULL, text=True
        )
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


# ── 브라우저 초기화 ────────────────────────────────────────────────────────────
def make_driver(profile_dir: str) -> uc.Chrome:
    opts = uc.ChromeOptions()
    opts.binary_location = CHROME_BIN
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--lang=en-US")
    if IS_CI:
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

    ver = get_chrome_major_version()
    print(f"  [Chrome {ver}]", flush=True)

    driver = uc.Chrome(
        driver_executable_path=DRIVER_PATH,
        options=opts,
        headless=False,
        version_main=ver,
    )
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def rand_sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


# ── 안전한 페이지 로드 ────────────────────────────────────────────────────────
def safe_get(driver: uc.Chrome, url: str) -> str:
    try:
        driver.get(url)
    except Exception:
        pass
    time.sleep(CF_WAIT)
    try:
        return driver.page_source
    except Exception:
        return ""


# ── 파싱: 기관 보유 비중 ───────────────────────────────────────────────────────
def parse_institutional(html: str) -> str:
    m = re.search(
        r'Institutional Shares \(Long\)</td>\s*<td>[^<]*?-\s*([\d,]+\.?\d*)\s*%',
        html, re.DOTALL,
    )
    return f"{m.group(1).replace(',', '')}%" if m else "N/A"


# ── 파싱: CTB ─────────────────────────────────────────────────────────────────
def parse_ctb(html: str) -> str:
    m_table = re.search(
        r'id="table-short-borrow-rate".*?<tbody>(.*?)</tbody>',
        html, re.DOTALL,
    )
    if not m_table:
        return "N/A"
    m_row = re.search(r'<tr>(.*?)</tr>', m_table.group(1), re.DOTALL)
    if not m_row:
        return "N/A"
    nums = re.findall(r'class="table-numeric">\s*([\d.]+)\s*</td>', m_row.group(1))
    if len(nums) >= 4:
        return f"{nums[3]}%"
    return f"{nums[-1]}%" if nums else "N/A"


# ── Fintel 스크래핑 ────────────────────────────────────────────────────────────
def scrape_fintel() -> List[StockData]:
    results: List[StockData] = []
    total = len(TICKERS)
    driver = None
    profile_dir = None

    print(f"\n[*] Fintel.io 스크래핑 시작 — {total}개 종목\n")

    try:
        for idx, ticker in enumerate(TICKERS, 1):
            if (idx - 1) % RESTART_EVERY == 0:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                if profile_dir and os.path.exists(profile_dir):
                    shutil.rmtree(profile_dir, ignore_errors=True)
                profile_dir = tempfile.mkdtemp(prefix="cf_chrome_")
                driver = make_driver(profile_dir)
                time.sleep(4)
                if idx > 1:
                    print(f"\n  [브라우저 재시작 — 새 세션]\n")

            print(f"  [{idx:02d}/{total}] {ticker:<6}", end="  ", flush=True)
            data = StockData(ticker=ticker)

            html_so = safe_get(driver, f"https://fintel.io/so/us/{ticker}")
            if html_so:
                data.inst_pct = parse_institutional(html_so)

            rand_sleep(2.0, 3.5)

            html_ss = safe_get(driver, f"https://fintel.io/ss/us/{ticker}")
            if html_ss:
                data.ctb = parse_ctb(html_ss)

            results.append(data)
            print(f"기관: {data.inst_pct:>10}  |  CTB: {data.ctb:>8}", flush=True)

            if idx < total:
                rand_sleep(3.0, 5.0)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if profile_dir and os.path.exists(profile_dir):
            shutil.rmtree(profile_dir, ignore_errors=True)

    return results


# ── Google Sheets 업데이트 ────────────────────────────────────────────────────
def update_sheets(results: List[StockData]) -> None:
    fintel_data = {
        r.ticker: (
            r.inst_pct if r.inst_pct != "N/A" else "-",
            r.ctb      if r.ctb      != "N/A" else "-",
        )
        for r in results
    }

    # CI: 환경변수에서 직접 로드 (파일 쓰기 불필요) / 로컬: JSON 파일
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if IS_CI and sa_json_str:
        import json as _json
        creds = Credentials.from_service_account_info(
            _json.loads(sa_json_str), scopes=SCOPES
        )
    else:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)

    service = build("sheets", "v4", credentials=creds)
    sheets  = service.spreadsheets()

    meta        = sheets.get(spreadsheetId=SPREADSHEET_ID).execute()
    first_sheet = meta["sheets"][0]["properties"]["title"]

    a_col = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{first_sheet}'!A:A",
    ).execute().get("values", [])

    ticker_row: dict = {}
    for row_idx, cell in enumerate(a_col, start=1):
        if cell:
            t = str(cell[0]).strip().upper()
            if t in fintel_data:
                ticker_row[t] = row_idx

    print(f"\n[Sheets] '{first_sheet}' 시트에서 {len(ticker_row)}개 티커 매핑")

    value_ranges = []
    for ticker, row_num in sorted(ticker_row.items(), key=lambda x: x[1]):
        inst_val, ctb_val = fintel_data[ticker]
        value_ranges.append({
            "range": f"'{first_sheet}'!{COL_INST}{row_num}",
            "values": [[inst_val]],
        })
        value_ranges.append({
            "range": f"'{first_sheet}'!{COL_CTB}{row_num}",
            "values": [[ctb_val]],
        })

    resp = sheets.values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": value_ranges},
    ).execute()

    print(f"[Sheets] {resp.get('totalUpdatedCells', 0)}개 셀 업데이트 완료\n")


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main() -> None:
    results = scrape_fintel()
    update_sheets(results)
    print("[*] 모든 작업 완료!")


if __name__ == "__main__":
    main()
