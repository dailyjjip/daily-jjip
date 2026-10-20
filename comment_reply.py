"""정보성 질문 댓글에만 짧게 답글 (운영자 지시 10/20)

- 하루 최대 3개. 일반 댓글(칭찬·감상·의견·인사)에는 답하지 않음. 너무 많은 소통은 지양.
- 게시물에 정리된 내용(data.json)으로 확실히 답할 수 있는 질문만. 애매하면 답하지 않음.
- 말투: AI 티 안 나게, 정보 주듯 담백하게 1~2문장. 인사·감사·이모지·"좋은 질문" 류 금지.
- 이미 판단한 댓글은 comments.json에 기록해 다시 비용 쓰지 않음. 계정 보호 안전장치(일시정지·경고) 준수.
"""
import datetime
import json
import os
import random
import time

import requests

import main as M

STATE = "comments.json"
DAILY_MAX = int(os.getenv("REPLY_DAILY_MAX", "3"))
DRY = os.getenv("REPLY_DRY", "false") == "true"  # true면 댓글·답글 초안만 출력(게시·기록 안 함)
API = "https://graph.instagram.com/v21.0"
KST = datetime.timedelta(hours=9)


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"seen": [], "replies": []}


def today_kst():
    return (datetime.datetime.utcnow() + KST).strftime("%Y%m%d")


KEYS = ("headline", "kicker", "what", "numbers", "details", "points", "watch", "sources")


def post_info():
    """최근 게시물 20개(운영자가 직접 올린 릴스 포함, 댓글 있는 것만): media id → 게시물 정보"""
    by_id, by_cap = {}, {}
    for p in json.load(open(M.HISTORY, encoding="utf-8")):
        path = os.path.join(M.OUT_DIR, p.get("date", ""), "data.json")
        if p.get("post_id") and os.path.exists(path):
            by_id[str(p["post_id"])] = path
    for base in (M.OUT_DIR, "reels"):  # 캡션 첫 부분으로도 찾기 (직접 올린 릴스)
        for dname in os.listdir(base) if os.path.isdir(base) else []:
            path = os.path.join(base, dname, "data.json")
            if os.path.exists(path):
                try:
                    by_cap[json.load(open(path, encoding="utf-8")).get("caption", "")[:30]] = path
                except Exception:
                    pass
    r = requests.get(f"{API}/me/media", params={"fields": "id,caption,comments_count,timestamp", "limit": 20,
                                               "access_token": M.IG_TOKEN}, timeout=30).json()
    out = {}
    for m in r.get("data", []):
        if not m.get("comments_count"):
            continue
        cap = m.get("caption") or ""
        path = by_id.get(m["id"]) or by_cap.get(cap[:30])
        out[m["id"]] = ({k: json.load(open(path, encoding="utf-8")).get(k) for k in KEYS} if path
                        else {"headline": cap.split("\n")[0][:80], "caption": cap[:1200]})
    if DRY:
        print(f"::notice::댓글 있는 게시물 {len(out)}개 (조회 응답: {str(r)[:150] if 'error' in r else 'ok'})")
    return out


def judge(items):
    """질문인지·게시물 정보로 답할 수 있는지·답글 문구를 한 번에 판단"""
    from anthropic import Anthropic
    prompt = """인스타 뉴스 계정 '데일리찝' 운영자로서 댓글을 분류하고, 필요한 경우에만 답글을 쓰세요.

답글 대상: 게시물 내용에 대해 정보를 묻는 질문(사실·숫자·배경·의미·앞으로 어떻게 되는지 등)이고,
아래 게시물 정보만으로 정확히 답할 수 있는 것만. 그 외(칭찬, 감상, 의견, 토론 A/B 선택, 인사, 농담, 비난, 광고, 정치)는 모두 답하지 않음.
게시물 정보로 확실히 답할 수 없으면 답하지 않음(추측 금지).

답글 말투: 사람이 정보 알려주듯 담백하게 1~2문장, 60자 안팎. 반말·과한 존댓말 말고 "~예요/~입니다" 정도.
금지: 인사("안녕하세요"), 감사("댓글 감사합니다"), "좋은 질문", 이모지, 느낌표 남발, 해시태그, 링크, "AI" 언급.

JSON 배열만 출력: [{"id":"댓글id","reply":true/false,"text":"답글(reply=false면 빈 문자열)"}]

"""
    for it in items:
        prompt += f"\n[댓글 id={it['id']}] @{it['username']}: {it['text']}\n게시물 정보: {json.dumps(it['post'], ensure_ascii=False)}\n"
    msg = Anthropic().messages.create(model=M.REVIEW_MODEL, max_tokens=6000,
                                      messages=[{"role": "user", "content": prompt}])
    M.log_cost(msg, "comment_reply")
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return {str(x["id"]): x for x in json.loads(txt[txt.index("["): txt.rindex("]") + 1])}


def run():
    if not M.IG_TOKEN:
        print("토큰 없음 → 건너뜀"); return
    h = M.load_health()
    if h.get("paused_until") and datetime.datetime.utcnow().isoformat() < h["paused_until"]:
        print("계정 보호 일시정지 중 → 답글 건너뜀"); return
    st = load_state()
    done_today = [r for r in st["replies"] if r.get("day") == today_kst()]
    if len(done_today) >= DAILY_MAX:
        print(f"오늘 답글 {len(done_today)}개 → 한도 {DAILY_MAX} 도달"); return
    posts = post_info()
    seen = set(st["seen"])
    new = []
    for pid, info in posts.items():
        r = requests.get(f"{API}/{pid}/comments", params={"fields": "id,text,username,timestamp,replies{username}",
                                                         "access_token": M.IG_TOKEN}, timeout=30).json()
        if "error" in r:
            e = r["error"]
            if e.get("code") in (10, 200) or "permission" in str(e).lower():
                if not any(x.get("kind") == "comments_perm" for x in h.get("events", [])[-30:]):
                    M.health_event("comments_perm", e.get("message", ""))
                print("댓글 권한 없음(instagram_business_manage_comments 필요) → 건너뜀"); return
            print("댓글 조회 오류:", e)
            if DRY:
                print(f"::notice::댓글 조회 오류: {str(e)[:200]}")
            continue
        for c in r.get("data", []):
            if c["id"] in seen or c.get("username") == "daily.jjip":
                continue
            if any(x.get("username") == "daily.jjip" for x in (c.get("replies") or {}).get("data", [])):
                seen.add(c["id"]); continue
            new.append({"id": c["id"], "username": c.get("username", ""), "text": c.get("text", ""), "post": info})
    if not new:
        print("::notice::새 댓글 없음" if DRY else "새 댓글 없음"); return
    verdict = judge(new[:20])
    if DRY:
        for it in new[:20]:
            v = verdict.get(it["id"], {})
            print(f"::notice::댓글 [{it['post'].get('headline')}] @{it['username']}: {it['text']} → {'답글: ' + v.get('text', '') if v.get('reply') else '답 안 함'}")
        return
    left = DAILY_MAX - len(done_today)
    for it in new[:20]:
        seen.add(it["id"])
        v = verdict.get(it["id"])
        if not v or not v.get("reply") or not v.get("text") or left <= 0:
            continue
        r = requests.post(f"{API}/{it['id']}/replies", data={"message": v["text"], "access_token": M.IG_TOKEN}, timeout=30).json()
        if "id" not in r:
            M.health_event("blocked_warning" if r.get("error", {}).get("code") in (4, 9, 17, 368) else "reply_fail", r)
            print("답글 실패 → 중단:", r); break
        st["replies"].append({"day": today_kst(), "comment": it["text"][:100], "reply": v["text"], "reply_id": r["id"]})
        print("답글:", it["text"][:40], "→", v["text"])
        left -= 1
        if left > 0:
            time.sleep(random.randint(90, 240))  # 연속 답글 간격 (스팸 판정 방지)
    st["seen"] = list(seen)[-3000:]
    st["replies"] = st["replies"][-300:]
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    run()
