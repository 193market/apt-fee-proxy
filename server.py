"""
관리비탐정 Proxy Server
- K-APT 웹사이트 → kaptCode 조회 (getKaptList.do)
- AptMgCostInfoServiceV2 → 실제 관리비 조회
- 결과 메모리 캐시 (동일 요청 반복 방지)
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import xml.etree.ElementTree as ET
import time
import json
import re
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

API_KEY = "xsG0WMPtWS1mUarzKPkfhWjUUvyKIqfBF34M5NHtM7PcQykB9r9bfji96dhrfkH0peDerZ6iDfVqwSoYS9SEcQ=="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 메모리 캐시
_cache = {}


def get_cache(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < 3600:  # 1시간 캐시
        return entry["data"]
    return None


def set_cache(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}


def get_kapt_session():
    """K-APT 웹사이트 세션 + CSRF 토큰 획득"""
    cache_key = "_kapt_session"
    entry = _cache.get(cache_key)
    # 세션은 30분 캐시
    if entry and time.time() - entry["ts"] < 1800:
        return entry["data"]

    session = requests.Session()
    try:
        r = session.get(
            "https://www.k-apt.go.kr/web/main/index.do",
            headers=HEADERS,
            timeout=15
        )
        r.encoding = "utf-8"
        csrf_match = re.search(r'name="_csrf"[^>]+content="([^"]+)"', r.text)
        csrf = csrf_match.group(1) if csrf_match else ""
        result = {"session": session, "csrf": csrf}
        _cache[cache_key] = {"data": result, "ts": time.time()}
        print(f"K-APT session initialized, csrf={csrf[:8]}...")
        return result
    except Exception as e:
        print(f"K-APT session init failed: {e}")
        return None


def fetch_kapt_list(sgg_cd: str) -> list[dict]:
    """
    K-APT 웹사이트에서 시군구 내 전체 단지 목록 조회
    sgg_cd: 5자리 시군구코드 (예: 11680)
    """
    cache_key = f"kaptlist_{sgg_cd}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    sess_data = get_kapt_session()
    if not sess_data:
        return []

    session = sess_data["session"]
    csrf = sess_data["csrf"]

    try:
        r = session.post(
            "https://www.k-apt.go.kr/kaptinfo/getKaptList.do",
            data={"bjdCode": sgg_cd},
            headers={
                **HEADERS,
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://www.k-apt.go.kr/web/main/index.do",
            },
            timeout=15
        )
        r.encoding = "utf-8"
        data = r.json()
        items = data.get("resultList") or []
        if isinstance(items, list) and items and items[0] == "":
            items = []
        set_cache(cache_key, items)
        print(f"kaptList for {sgg_cd}: {len(items)} items")
        return items
    except Exception as e:
        print(f"getKaptList.do failed for {sgg_cd}: {e}")
        # 세션 무효화 후 재시도
        _cache.pop("_kapt_session", None)
        return []


def jibun_to_bun(jibun: str):
    """지번 문자열 → (bun1, bun2) 튜플. 예: '651-1' → ('0651', '0001')"""
    parts = jibun.strip().split('-')
    bun1 = parts[0].zfill(4)
    bun2 = parts[1].zfill(4) if len(parts) > 1 else '0000'
    return bun1, bun2


def find_kapt_code(sgg_cd: str, apt_nm: str, jibun: str = "", build_year: str = "") -> str | None:
    """
    K-APT 웹사이트 getKaptList.do 를 통해 kaptCode 조회
    sgg_cd: 5자리 시군구코드 (예: 11680)
    apt_nm: 아파트명 (kaptName 난독화로 이름 매칭 불가 → 보조 정보로만 활용)
    jibun: 번지 (예: '651' 또는 '651-1') — 주 매칭 키
    build_year: 건축년도 (예: '2019') — jibun 없을 때 보조 매칭
    """
    cache_key = f"kapt_{sgg_cd}_{apt_nm}_{jibun}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    items = fetch_kapt_list(sgg_cd)
    if not items:
        print(f"kaptCode not found (empty list) for {apt_nm} ({sgg_cd})")
        return None

    # 전략 1: jibun(번지) + sggCd로 bun1/bun2 매칭 (가장 신뢰성 높음)
    if jibun:
        target_bun1, target_bun2 = jibun_to_bun(jibun)
        for item in items:
            k_bun1 = (item.get("bun1") or "").strip()
            k_bun2 = (item.get("bun2") or "").strip()
            k_code = item.get("kaptCode", "")
            bj_code = item.get("bjdCode", "")
            if k_code and k_bun1 == target_bun1 and k_bun2 == target_bun2 and bj_code.startswith(sgg_cd):
                set_cache(cache_key, k_code)
                print(f"kaptCode by jibun: {k_code} (bun={k_bun1}-{k_bun2}) for {apt_nm}")
                return k_code
        # 번지 주번지만 매칭 (부번지 다를 수 있음)
        for item in items:
            k_bun1 = (item.get("bun1") or "").strip()
            k_code = item.get("kaptCode", "")
            bj_code = item.get("bjdCode", "")
            if k_code and k_bun1 == target_bun1 and bj_code.startswith(sgg_cd):
                set_cache(cache_key, k_code)
                print(f"kaptCode by bun1 only: {k_code} (bun1={k_bun1}) for {apt_nm}")
                return k_code

    # 전략 2: 건축년도 매칭 (같은 구, 같은 해 건축 단지 중 첫 번째)
    if build_year:
        year_matches = [
            item for item in items
            if item.get("occuFirstDate", "")[:4] == build_year
            and item.get("bjdCode", "").startswith(sgg_cd)
        ]
        if len(year_matches) == 1:
            k_code = year_matches[0].get("kaptCode", "")
            set_cache(cache_key, k_code)
            print(f"kaptCode by buildYear: {k_code} for {apt_nm} ({build_year})")
            return k_code

    print(f"kaptCode not found for {apt_nm} ({sgg_cd}) jibun={jibun} year={build_year}")
    return None


def call_api(url: str, params: dict) -> str | None:
    """공공데이터 API 호출"""
    params["serviceKey"] = API_KEY
    try:
        res = requests.get(url, params=params, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            return res.text
    except Exception as e:
        print(f"API call failed: {e}")
    return None


def parse_items(xml_str: str) -> list[dict]:
    """XML 응답에서 item 목록 파싱"""
    try:
        root = ET.fromstring(xml_str)
        result_code = root.findtext(".//resultCode", "")
        if result_code and result_code != "000":
            print(f"API resultCode: {result_code} - {root.findtext('.//resultMsg', '')}")
            return []
        items = root.findall(".//item")
        return [{child.tag: child.text for child in item} for item in items]
    except Exception as e:
        print(f"XML parse error: {e}")
        return []


def get_mgcost(kapt_code: str) -> dict | None:
    """
    관리비 실데이터 조회
    최근 6개월 중 데이터 있는 월 반환
    """
    cache_key = f"mgcost_{kapt_code}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    BASE = "https://apis.data.go.kr/1613000/AptMgCostInfoServiceV2/getAptMgCostInfo"

    for months_ago in range(1, 7):
        d = datetime.now().replace(day=1) - timedelta(days=months_ago * 28)
        year = str(d.year)
        month = str(d.month).zfill(2)

        xml = call_api(BASE, {"kaptCode": kapt_code, "searchYear": year, "searchMonth": month})
        if xml:
            items = parse_items(xml)
            if items:
                result = items[0]
                result["year"] = year
                result["month"] = month
                set_cache(cache_key, result)
                print(f"mgcost found for {kapt_code}: {year}-{month}")
                return result

    # 개별사용료 API도 시도
    BASE2 = "https://apis.data.go.kr/1613000/AptIndvdlzManageCostServiceV2/getAptIndvdlzManageCostInfo"
    for months_ago in range(1, 7):
        d = datetime.now().replace(day=1) - timedelta(days=months_ago * 28)
        year = str(d.year)
        month = str(d.month).zfill(2)
        xml = call_api(BASE2, {"kaptCode": kapt_code, "searchYear": year, "searchMonth": month})
        if xml:
            items = parse_items(xml)
            if items:
                result = items[0]
                result["year"] = year
                result["month"] = month
                result["source"] = "individual"
                set_cache(cache_key, result)
                return result

    print(f"mgcost not found for {kapt_code}")
    return None


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/mgcost")
def api_mgcost():
    """
    관리비 조회 엔드포인트
    Query params: sggCd, aptNm, jibun (번지), buildYear
    """
    sgg_cd = request.args.get("sggCd", "").strip()
    apt_nm = request.args.get("aptNm", "").strip()
    jibun = request.args.get("jibun", "").strip()
    build_year = request.args.get("buildYear", "").strip()

    if not sgg_cd or not apt_nm:
        return jsonify({"error": "sggCd and aptNm required"}), 400

    kapt_code = find_kapt_code(sgg_cd, apt_nm, jibun, build_year)
    if not kapt_code:
        return jsonify({"error": "kaptCode not found", "aptNm": apt_nm}), 404

    mgcost = get_mgcost(kapt_code)
    if not mgcost:
        return jsonify({
            "error": "mgcost not found",
            "kaptCode": kapt_code,
            "aptNm": apt_nm
        }), 404

    return jsonify({
        "kaptCode": kapt_code,
        "aptNm": apt_nm,
        "data": mgcost
    })


@app.route("/api/mgcost-batch")
def api_mgcost_batch():
    """
    여러 아파트 일괄 조회
    Query params: sggCd, apts (JSON array of aptNm strings)
    """
    sgg_cd = request.args.get("sggCd", "").strip()
    apts_raw = request.args.get("apts", "[]")

    try:
        apt_names = json.loads(apts_raw)
    except Exception:
        return jsonify({"error": "invalid apts param"}), 400

    results = []
    for apt_nm in apt_names[:10]:  # 최대 10개
        kapt_code = find_kapt_code(sgg_cd, apt_nm)
        if kapt_code:
            mgcost = get_mgcost(kapt_code)
            results.append({
                "aptNm": apt_nm,
                "kaptCode": kapt_code,
                "mgcost": mgcost
            })
        else:
            results.append({
                "aptNm": apt_nm,
                "kaptCode": None,
                "mgcost": None
            })

    return jsonify({"results": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
