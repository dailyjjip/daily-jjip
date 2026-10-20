"""업종 특화 입찰 공고 알리미 (엔바이록스 내부 검증용, 2호 사업 후보)

매 평일 아침: 나라장터 공고(조달청 공공데이터 개방표준 API) 수집 → 업종 키워드 1차 필터(무료)
→ Claude Haiku가 적합도·참가 가능성 한 줄 평가 → bids/digest/<날짜>.md + GitHub 이슈(메일 알림)
"""
import datetime
import json
import os
import re
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API = "https://apis.data.go.kr/1230000/ao/PubDataOpnStdService/getDataSetOpnStdBidPblancInfo"
KEY = os.getenv("DATA_GO_KR_KEY", "").strip()
MODEL = os.getenv("BID_MODEL", "claude-haiku-5-5")
STATE = os.path.join(HERE, "state.json")
COSTS = os.path.join(ROOT, "costs.json")  # 데일리찝과 같은 예산 장부 → AI 사업부 전체 상한에 함께 잡힘
BUDGET_KRW = 45000
KST = datetime.timezone(datetime.timedelta(hours=9))

# 회사 프로필 (운영자가 고치면 바로 반영)
PROFILE = ("엔바이록스: 경기도 안산 소재 4~5인 환경설비 제작·설치 업체. 주력: RTO(축열식 소각설비)·VOC 처리, "
           "집진기(백필터), 스크러버(세정탑), 산업용 덕트 제작·설치, 제관·탱크, 철구조물, 플랜트 배관.")
INCLUDE = ["RTO", "축열", "소각설비", "VOC", "휘발성유기", "대기오염방지", "방지시설", "집진", "백필터", "여과집진",
           "스크러버", "세정탑", "세정시설", "흡착", "활성탄", "악취", "탈취", "덕트", "닥트", "환기설비", "배기설비",
           "국소배기", "제관", "탱크", "저장조", "철구조물", "강구조물", "배관", "플랜트", "송풍기", "블로워", "분진"]
EXCLUDE = ["청소용역", "급식", "인쇄", "소프트웨어", "홍보", "교육용역"]


def now_kst():
    return datetime.datetime.now(KST)


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def fetch(begin, end):
    """기간 내 공고 전부(페이지 반복). 응답 구조가 바뀌어도 item 목록을 찾아낸다."""
    out, page = [], 1
    while page <= 30:
        r = requests.get(API, params={"serviceKey": KEY, "type": "json", "numOfRows": 500, "pageNo": page,
                                      "bidNtceBgnDt": begin, "bidNtceEndDt": end}, timeout=60)
        try:
            j = r.json()
        except Exception:
            raise RuntimeError(f"API 응답이 JSON이 아님 (HTTP {r.status_code}): {r.text[:300]}")
        body = (j.get("response") or {}).get("body") or j.get("body") or {}
        if not body:
            raise RuntimeError(f"API 오류: {str(j)[:300]}")
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        out += items
        total = int(body.get("totalCount") or 0)
        if not items or len(out) >= total:
            break
        page += 1
    return out


def g(it, *keys):
    for k in keys:
        v = it.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def bid_id(it):
    return g(it, "bidNtceNo") + "-" + g(it, "bidNtceOrd")


def match(it):
    name = g(it, "bidNtceNm")
    text = name or " ".join(str(v) for v in it.values())
    if any(x in text for x in EXCLUDE):
        return []
    return [k for k in INCLUDE if k.lower() in text.lower()]


def won(v):
    try:
        n = float(re.sub(r"[^\d.]", "", v))
        return f"{n / 1e8:.2f}억" if n >= 1e8 else f"{n / 1e4:,.0f}만"
    except Exception:
        return "-"


def spend_30d():
    cut = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat()
    return sum(x.get("krw", 0) for x in load(COSTS, []) if x.get("at", "") >= cut)


def ai_review(cands):
    """적합도 1~5 + 한 줄 이유 + 주의점. 실패하거나 예산 초과면 키워드 결과만 보낸다."""
    if not cands or not os.getenv("ANTHROPIC_API_KEY") or spend_30d() >= BUDGET_KRW:
        return {}
    from anthropic import Anthropic
    rows = "\n".join(
        f"[{i}] {g(c, 'bidNtceNm')} | 기관 {g(c, 'ntceInsttNm', 'dminsttNm', 'dmndInsttNm')} | "
        f"구분 {g(c, 'bsnsDivNm', 'bidNtceSttusNm')} | 금액 {won(g(c, 'presmptPrce', 'asignBdgtAmt', 'bdgtAmt'))} | "
        f"지역제한 {g(c, 'prtcptLmtRgnNm', 'rgnLmtBidLocplcJdgmBssNm', 'cnstrtsiteRgnNm') or '-'} | "
        f"업종제한 {g(c, 'indstrytyLmtNm', 'indstrytyNm', 'prtcptPsblRgnNm') or '-'}"
        for i, c in enumerate(cands[:40]))
    prompt = f"""{PROFILE}

아래 나라장터 입찰 공고가 이 회사에 맞는지 평가하세요.
score: 5=주력 업무 그대로 / 4=충분히 수행 가능 / 3=일부 공종만 해당 / 2=관련 낮음 / 1=무관(키워드만 겹침)
why: 왜 맞는지 한 줄(20자 내외). caution: 지역·업종·규모 제한 등 참가 전 확인할 점 한 줄(없으면 빈 문자열).
JSON 배열만 출력: [{{"i":0,"score":4,"why":"...","caution":"..."}}]

{rows}"""
    try:
        msg = Anthropic().messages.create(model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        u = msg.usage
        usd = u.input_tokens * 1e-6 + u.output_tokens * 5e-6  # Haiku 추정 단가
        costs = load(COSTS, [])
        costs.append({"at": datetime.datetime.utcnow().isoformat(timespec="seconds"), "purpose": "bid_alert",
                      "usd": round(usd, 4), "krw": round(usd * 1450), "in": u.input_tokens, "out": u.output_tokens, "search": 0})
        json.dump(costs[-2000:], open(COSTS, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        arr = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
        return {int(x["i"]): x for x in arr}
    except Exception as e:
        print("AI 평가 실패(키워드 결과만 보냄):", e)
        return {}


def run():
    if not KEY:
        print("DATA_GO_KR_KEY 미등록 → 건너뜀 (공공데이터포털 키를 GitHub Secrets에 넣으면 시작)")
        return
    st = load(STATE, {"last_end": None, "seen": []})
    end = now_kst()
    begin = (datetime.datetime.strptime(st["last_end"], "%Y%m%d%H%M").replace(tzinfo=KST)
             if st.get("last_end") else end - datetime.timedelta(days=1))
    begin = max(begin, end - datetime.timedelta(days=6))  # API 조회 기간 제한(1주) 대비
    items = fetch(begin.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M"))
    if items and not os.path.exists(os.path.join(HERE, "sample_item.json")):
        json.dump(items[0], open(os.path.join(HERE, "sample_item.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    seen = set(st.get("seen", []))
    cands = []
    for it in items:
        if bid_id(it) in seen:
            continue
        kw = match(it)
        if kw:
            it["_kw"] = kw
            cands.append(it)
            seen.add(bid_id(it))
    rev = ai_review(cands)
    ranked = sorted(enumerate(cands), key=lambda p: -(rev.get(p[0], {}).get("score", 3)))
    ranked = [(i, c) for i, c in ranked if rev.get(i, {}).get("score", 3) >= 3]  # 무관(1~2점)은 제외
    day = end.strftime("%Y%m%d")
    lines = [f"# 입찰 공고 알리미 {end:%m/%d} ({begin:%m/%d %H:%M}~{end:%m/%d %H:%M})",
             f"전체 {len(items):,}건 중 업종 해당 {len(ranked)}건\n"]
    for i, c in ranked:
        r = rev.get(i, {})
        star = "★" * int(r.get("score", 3)) if r else "(키워드)"
        url = g(c, "bidNtceUrl", "bidNtceDtlUrl")
        lines += [f"### {star} {g(c, 'bidNtceNm')}",
                  f"- 기관: {g(c, 'ntceInsttNm', 'dminsttNm', 'dmndInsttNm')} · 금액: {won(g(c, 'presmptPrce', 'asignBdgtAmt', 'bdgtAmt'))}"
                  f" · 마감: {g(c, 'bidClseDate', 'bidClseDt')} {g(c, 'bidClseTm')}",
                  f"- 왜: {r.get('why', ', '.join(c['_kw']))}" + (f" / ⚠ {r['caution']}" if r.get("caution") else ""),
                  f"- 공고: {url}" if url else f"- 공고번호: {bid_id(c)}", ""]
    os.makedirs(os.path.join(HERE, "digest"), exist_ok=True)
    md = "\n".join(lines)
    open(os.path.join(HERE, "digest", f"{day}.md"), "w", encoding="utf-8").write(md)
    open(os.path.join(HERE, "issue.md"), "w", encoding="utf-8").write(md if ranked else "")
    st["last_end"] = end.strftime("%Y%m%d%H%M")
    st["seen"] = list(seen)[-5000:]
    st.setdefault("log", []).append({"date": day, "total": len(items), "matched": len(cands), "sent": len(ranked)})
    st["log"] = st["log"][-120:]
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(md)


if __name__ == "__main__":
    run()
