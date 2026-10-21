"""
매일 이슈 카드뉴스 자동 생성 + 인스타그램 게시
흐름: 구글뉴스 RSS 수집 → Claude로 이슈 선정·문구 작성 → 비주얼(사진 또는 3D 아이콘) 합성 → 카드 이미지 → 인스타 게시
"""
import os, io, json, datetime, time, random, re
import feedparser, requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------- 설정 ----------
RSS_URL = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
MODEL = "claude-sonnet-5-5"
IG_USER_ID = os.getenv("IG_USER_ID")
IG_TOKEN = os.getenv("IG_ACCESS_TOKEN")
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL")              # 예: https://raw.githubusercontent.com/아이디/저장소/main/posts
PEXELS_KEY = os.getenv("PEXELS_API_KEY")
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY")                # 무료 사진(상업적 이용 가능, 출처 표기 불필요)                  # 있으면 무료 상업용 사진 사용, 없으면 3D 아이콘
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"  # true면 이미지만 만들고 게시 안 함
EMOJI_DIR = os.getenv("EMOJI_DIR", "emoji/package/assets")  # Fluent 3D 이모지(MIT 라이선스)
HISTORY, PENDING, OUT_DIR = "posted.json", "pending.json", "posts"
HANDLE = "@daily.jjip"

FONT_BLACK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"
if not os.path.exists(FONT_REG):
    FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
KR = 1  # ttc 안의 한국어 폰트 인덱스

W, H = 1080, 1350  # 인스타 세로형 4:5
# 테마: (배경 위, 배경 아래, 강조색, 글자색)
THEMES = [
    ("#3A0CA3", "#0D0221", "#FFE500", "#FFFFFF"),   # 보라 → 블랙, 형광 노랑
    ("#FF4D2E", "#2B0A05", "#FFE500", "#FFFFFF"),   # 레드 오렌지
    ("#00B4D8", "#03045E", "#FFE500", "#FFFFFF"),   # 블루
    ("#06D6A0", "#073B4C", "#FFE500", "#FFFFFF"),   # 그린
]


def font(path, size):
    return ImageFont.truetype(path, size, index=KR)


# ---------- 데이터 ----------
def load_history():
    if os.path.exists(HISTORY):
        return json.load(open(HISTORY, encoding="utf-8"))
    return []


SECTIONS = ["BUSINESS", "NATION", "ENTERTAINMENT", "SPORTS", "TECHNOLOGY"]


def fetch_news(n=30):
    """주요 뉴스 + 분야별(경제·사회·연예·스포츠·IT) 헤드라인 (무료 RSS)"""
    urls = [RSS_URL] + [f"https://news.google.com/rss/headlines/section/topic/{s}?hl=ko&gl=KR&ceid=KR:ko" for s in SECTIONS]
    seen, out = set(), []
    for i, u in enumerate(urls):
        try:
            for e in feedparser.parse(u).entries[: (n if i == 0 else 12)]:
                if e.title not in seen:
                    seen.add(e.title); out.append({"title": e.title, "link": e.link})
        except Exception:
            pass
    return out


BOOST_UNTIL = "20261025"  # 이 날짜(한국시간)까지 하루 6회 참여도 집중 기간


def in_boost():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y%m%d") <= BOOST_UNTIL


FOCUS_OVERRIDE = None  # 릴스 전용 등 특정 방향을 강제할 때


def slot_focus():
    """게시 시간대별 주제 방향 (한국시간)"""
    if FOCUS_OVERRIDE:
        return FOCUS_OVERRIDE
    h = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).hour
    if in_boost():
        # 집중 기간: 연애·스포츠·경제·과학·사회, 참여도 우선 (정치 제외)
        if h < 10:
            return "사회·경제: 오늘 아침 사람들이 깜짝 놀랄 사실이 담긴 사회·경제 뉴스"
        if h < 13:
            return "연애·관계: 연애·결혼·이혼·데이팅 문화·세대별 연애 통계 등 공감·논쟁을 부르는 이야기(연예인 열애는 공식 발표·확인된 보도만)"
        if h < 16:
            return "경제: 내 지갑에 바로 닿는 충격적인 숫자나 반전이 있는 경제·부동산·주식·물가 뉴스"
        if h < 19:
            return "과학·기술: '이게 진짜야?' 싶은 놀라운 연구 결과·우주·건강 과학·신기술 뉴스(논문·공식 발표 근거)"
        if h < 22:
            return "스포츠: 오늘 가장 화제인 경기·기록·선수 이슈 중 반전이나 대기록이 있는 것"
        return "사회 화제: 오늘 댓글이 가장 많이 달린 사회 이슈 중 아직 다루지 않은 것"
    if h < 10:
        return "오늘의 중대 이슈: 경제·사회·국제 중 오늘 한국인이 가장 많이 이야기할 큰 뉴스"
    if h < 16:
        return "생활 밀착 이슈: 부동산·주식·재테크·물가·월급·취업·세금·IT 신제품처럼 내 돈과 생활에 직접 닿는 뉴스"
    return "재밌는 화제: 연예·스포츠·트렌드·온라인 화제처럼 퇴근길에 가볍게 보고 공유하고 싶은 뉴스(사망·사건사고 제외)"


def extract_json(text):
    """응답 글 속에서 headline이 들어있는 마지막 JSON 객체를 찾아낸다."""
    dec, found = json.JSONDecoder(), None
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, dict) and "headline" in obj:
                    found = obj
            except ValueError:
                pass
    if found is None:
        raise ValueError("응답에서 JSON을 찾지 못했습니다:\n" + text[-800:])
    return found


TOPICS = "topics.json"


def recent_topics(hours=96):
    """최근 생성한 모든 주제(캐러셀·릴스, 게시 성공 여부 무관)"""
    items = json.load(open(TOPICS, encoding="utf-8")) if os.path.exists(TOPICS) else []
    for path in (HISTORY, "reels_history.json"):
        if os.path.exists(path):
            for h in json.load(open(path, encoding="utf-8")):
                items.append({"date": h.get("date", ""), "headline": h.get("headline", ""), "topic_key": h.get("topic_key", "")})
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).strftime("%Y%m%d_%H%M")
    seen, out = set(), []
    for t in items:
        k = (t.get("headline"), t.get("topic_key"))
        if t.get("date", "") >= cutoff and k not in seen:
            seen.add(k); out.append(t)
    return out


def record_topic(d, kind="carousel"):
    items = json.load(open(TOPICS, encoding="utf-8")) if os.path.exists(TOPICS) else []
    items.append({"date": d["stamp"], "kind": kind, "headline": d["headline"],
                  "topic_key": d.get("topic_key", ""), "category": d.get("category", "")})
    json.dump(items[-300:], open(TOPICS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _words(t):
    import re
    return {w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", t or "")}


def is_duplicate(d, recents):
    """같은 사건 판정: 주제 키워드가 2개 이상 겹치면 중복"""
    new = _words(d.get("topic_key", "")) | _words(d.get("headline", ""))
    for t in recents:
        old = _words(t.get("topic_key", "")) | _words(t.get("headline", ""))
        if len(new & old) >= 2 or (d.get("topic_key") and d.get("topic_key") == t.get("topic_key")):
            return t
    return None


def pick_issue(news, history):
    """Claude가 이슈 1개를 고르고, 웹 검색으로 사실을 확인해 슬라이드 6장 분량의 문구를 만든다."""
    from anthropic import Anthropic
    recents = recent_topics()
    done = "\n".join(f"- [{t.get('topic_key') or '-'}] {t['headline']}" for t in recents[-60:])
    items = "\n".join(f"- {x['title']}" for x in news)
    prompt = f"""오늘 한국 뉴스 헤드라인 목록입니다:
{items}

최근 4일간 이미 다룬 주제 [주제 키] 제목 — 같은 사건·같은 이슈는 각도를 바꿔도 금지(예: 같은 폭우를 강수량·침수·출근길로 나눠 다시 쓰기 금지):
{done or '(없음)'}

1) 이번 게시 방향은 [{slot_focus()}] 입니다. 이 방향에 맞으면서 한국인의 관심(검색·공유·댓글)이 가장 클 이슈 1개를 고르세요.
   우선순위: 오늘 실시간으로 화제인 것 > 쇼킹하거나 반전이 있어 공유하고 싶은 것 > 의견이 갈려 댓글이 달릴 만한 것 > 많은 사람의 돈·생활에 영향. 소수만 관심 가질 전문 뉴스는 피하세요.
   제목은 사실에 기반해 자극적으로: 충격적인 숫자, 반전, 대비(예: "월급은 3% 올랐는데 집값은 30%")를 앞세워 스크롤을 멈추게 쓰세요. 표현은 과감하게 과장·자극해도 됩니다(운영자 방침). 단 사실 자체(무엇이 일어났는지·숫자·대상)를 틀리게 만들거나 확인 안 된 일을 사실처럼 단정하면 안 됩니다.
2) web_search로 그 이슈의 최신 기사 2~3개를 확인해 구체적인 사실과 수치를 모으세요.
3) 매거진 스타일(아이즈매거진, 디에디트 같은 에디토리얼 톤) 인스타 캐러셀 문구를 쓰세요.
   순서: 표지 → 무슨 일이야 → 세부 내용 → 요약 및 앞으로 주목할 포인트 → 에디터 한 줄 + 토론 질문 → 팔로우

규칙:
- 검색으로 확인한 사실만 사용. 확인 안 된 수치는 쓰지 말 것. 추측·특정인 비방 금지(표현의 과장·자극은 허용).
- 정치 주제(정당·선거·정치인·정치 공방)는 고르지 말 것(별도 채널 예정). 사망·참사 이슈도 고르지 말 것. 토론 질문은 생활 속 찬반이 갈리는 질문으로.
- 유치한 감탄·밈 표현 금지. 짧고 단단한 문장, 잡지 헤드라인처럼 위트 있게.
- 데일리찝의 색깔: "남들이 놓친 한 지점을 콕 찝는다". 단순 요약이 아니라, 이 뉴스에서 독자가 진짜 궁금해할 관점 하나를 골라 그 관점으로 전체를 엮을 것.
- 독창성: 기사 문장을 그대로 옮기지 말고 반드시 자기 말로 다시 쓸 것. 에디터 한 줄은 다른 매체에서 볼 수 없는 데일리찝만의 해석이어야 함.
- 토론 질문은 양쪽 모두 합리적인 선택지로. 특정 집단·정당·인물 비난을 유도하거나 혐오를 부추기는 질문 금지.
- 같은 문장을 여러 장에서 반복하지 말 것. 글자 수 제한을 반드시 지킬 것.

마지막에 아래 JSON만 출력(코드블록 없이):
{{"category": "경제|사회|IT|생활|국제|스포츠|연예 중 하나",
 "kicker": "표지 머리말, 14자 이내",
 "headline": "표지 큰 제목, 16자 이내",
 "big_number": "이슈를 대표하는 숫자나 짧은 키워드, 6자 이내",
 "big_label": "big_number 설명, 14자 이내",
 "what": ["무슨 일인지 설명하는 문장 3개, 각 38자 이내"],
 "numbers": [{{"value": "핵심 수치, 7자 이내", "label": "설명, 12자 이내"}}, "... 3개"],
 "details": [{{"head": "세부 내용 소제목, 14자 이내", "body": "배경·맥락·구체적 사실, 44자 이내"}}, "... 3개"],
 "points": ["전체 요약 3개, 각 22자 이내"],
 "watch": [{{"head": "앞으로 주목할 포인트, 14자 이내", "body": "예정된 일정·발표·변수 위주, 40자 이내"}}, "... 2개"],
 "oneliner": "에디터 코멘트 한 문장, 26자 이내, 제목과 다른 관점",
 "debate": {{"question": "댓글 토론을 부르는 질문, 24자 이내. 찬반이 실제로 갈리는 생활 밀착형 질문", "a": "선택지 A, 12자 이내", "b": "선택지 B, 12자 이내"}},
 "image_queries": ["표지용 사진 검색 영어 키워드 2~4단어 (이슈를 한눈에 보여주는 실사 장면, 예: seoul apartment buildings). 기사 속 실제 장면을 묘사하는 구체적 표현(예: surgical robot operating room). 뜻이 여러 개라 엉뚱한 사진이 나올 단어 하나만 쓰지 말 것", "본문용 사진 검색 영어 키워드 2~4단어 (표지와 다른 장면, 같은 규칙)"],
 "caption": "첫 문장에 사람들이 검색할 핵심 키워드(예: 코스피 급락 이유)를 자연스럽게 넣고, 인스타 본문 3~4문장, 마지막에 저장/공유 유도 한 문장",
 "hashtags": ["#태그", "... 4개 (검색량 많은 핵심어 위주)"],
 "sources": ["참고한 언론사명 2~3개"],
 "topic_key": "이 이슈를 대표하는 핵심 사건 2~3단어 (예: 서울 가을 폭우, 한은 기준금리, 코스피 반대매매)",
 "source_title": "근거로 쓴 헤드라인 원문"}}"""
    client = Anthropic()
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    messages = [{"role": "user", "content": prompt}]
    data = None
    for _ in range(4):
        msg = client.messages.create(model=MODEL, max_tokens=8000, tools=tools, messages=messages)
        log_cost(msg, "carousel")
        messages.append({"role": "assistant", "content": msg.content})
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            data = extract_json(text)
            break
        except ValueError:
            pass
        if msg.stop_reason == "pause_turn":
            continue  # 검색 도중 멈춘 경우 이어서 진행
        messages.append({"role": "user", "content": "지금까지 확인한 내용으로 위에서 요청한 JSON만 출력하세요. 다른 말은 쓰지 마세요."})
    if data is None:
        raise ValueError("여러 번 시도했지만 JSON을 받지 못했습니다")
    dup = is_duplicate(data, recents)
    if dup and not getattr(pick_issue, "_retry", False):
        print("중복 주제 감지 → 다른 주제로 다시 선정:", data.get("topic_key"), "≈", dup.get("headline"))
        banned = _words(dup.get("topic_key", "")) | _words(data.get("topic_key", ""))
        news2 = [x for x in news if len(_words(x["title"]) & banned) < 2]
        pick_issue._retry = True
        try:
            return pick_issue(news2, history)
        finally:
            pick_issue._retry = False
    src = next((x for x in news if x["title"] == data.get("source_title")), None)
    data["source_link"] = src["link"] if src else ""
    return data


# ---------- 비주얼 소스 ----------
def emoji_img(ch):
    """이모지 문자 → Fluent 3D 이모지 PNG(256px)"""
    cps = [f"{ord(c):x}" for c in ch]
    for name in ("-".join(cps), "-".join(c for c in cps if c != "fe0f"), "-".join(cps) + "-fe0f"):
        p = os.path.join(EMOJI_DIR, name + ".webp")
        if os.path.exists(p):
            return Image.open(p).convert("RGBA")
    return None


STOP = {"a", "an", "the", "of", "in", "on", "and", "with", "for", "at", "to", "korea", "korean", "seoul"}


def photo_fit(query, tags):
    """사진 태그가 검색어와 얼마나 맞는지(보조 경로용). 주 경로는 AI가 기사 상황을 보고 고르는 choose_photos."""
    tagset = {t.strip().lower() for t in re.split(r"[,]", tags or "") if t.strip()}
    words = set(re.findall(r"[a-z]+", " ".join(tagset)))
    q = [w for w in re.findall(r"[a-z]+", query.lower()) if w not in STOP]
    hit = sum(1 for w in q if w in words or any(t.startswith(w[:5]) for t in words if len(w) >= 5))
    need = min(2, len(q))
    return hit if hit >= need else 0


def pixabay_photo(query, orientation="portrait", skip_ids=()):
    """Pixabay 무료 사진. 반환: (이미지, 출처 표기, 사진 id) 또는 None"""
    if not PIXABAY_KEY or not query:
        return None
    try:
        r = requests.get("https://pixabay.com/api/", params={
            "key": PIXABAY_KEY, "q": query, "image_type": "photo", "safesearch": "true",
            "orientation": "vertical" if orientation == "portrait" else "horizontal",
            "per_page": 10, "order": "popular"}, timeout=20).json()
        hits = [(photo_fit(query, h.get("tags", "")), h) for h in (r.get("hits") or []) if h["id"] not in skip_ids]
        hits = [x for x in hits if x[0] > 0]  # 검색어와 안 맞거나 상표 사진은 버림 → 없으면 다음 검색어/어두운 배경
        if not hits:
            print("맞는 사진 없음:", query)
            return None
        best = max(x[0] for x in hits)
        h = random.choice([x[1] for x in hits if x[0] == best][:3])
        img = Image.open(io.BytesIO(requests.get(h["largeImageURL"], timeout=30).content)).convert("RGB")
        return img, f"{h.get('user', '')} / Pixabay", h["id"]
    except Exception as e:
        print("Pixabay 사진 실패:", e)
        return None


def photo_candidates(query, orientation, n=6, exclude=()):
    """검색어로 사진 후보 목록(미리보기 URL 포함). Pixabay 우선, 부족하면 Pexels."""
    out = []
    try:
        if PIXABAY_KEY and query:
            r = requests.get("https://pixabay.com/api/", params={
                "key": PIXABAY_KEY, "q": query, "image_type": "photo", "safesearch": "true",
                "orientation": "vertical" if orientation == "portrait" else "horizontal",
                "per_page": 20, "order": "popular"}, timeout=20).json()
            for h in r.get("hits") or []:
                if h["id"] not in exclude:
                    out.append({"id": h["id"], "small": h.get("webformatURL"), "large": h["largeImageURL"],
                                "credit": f"{h.get('user', '')} / Pixabay", "tags": h.get("tags", "")})
    except Exception as e:
        print("Pixabay 후보 실패:", e)
    try:
        if PEXELS_KEY and query and len(out) < n:
            r = requests.get("https://api.pexels.com/v1/search", params={"query": query, "orientation": orientation,
                             "per_page": 10}, headers={"Authorization": PEXELS_KEY}, timeout=20).json()
            for p in r.get("photos") or []:
                if p["id"] not in exclude:
                    out.append({"id": p["id"], "small": p["src"]["medium"], "large": p["src"]["large2x"],
                                "credit": f"{p.get('photographer', '')} / Pexels", "tags": p.get("alt", "")})
    except Exception as e:
        print("Pexels 후보 실패:", e)
    return out[:n]


def choose_photos(d, exclude=(), need_cover=True, need_body=True):
    """기사 상황에 맞는 사진을 AI가 직접 보고 고른다(금지 목록 방식 아님).
    반환 (표지 사진, 본문 사진) — 각각 (PIL 이미지, 출처, id) 또는 None(맞는 게 없음 → 어두운 배경)"""
    import base64
    from anthropic import Anthropic
    qs = d.get("image_queries") or [d.get("image_query", "")]
    q2 = qs[1] if len(qs) > 1 else qs[0]
    sets = {}
    if need_cover:
        c = photo_candidates(qs[0], "portrait", 4, exclude)
        c += photo_candidates(q2, "portrait", 2, set(exclude) | {x["id"] for x in c})
        sets["C"] = c
    if need_body:
        b = photo_candidates(q2, "landscape", 4, exclude)
        b += photo_candidates(qs[0], "landscape", 2, set(exclude) | {x["id"] for x in b})
        sets["B"] = b
    content = []
    for key, lst in sets.items():
        for i, c in enumerate(lst):
            try:
                im = Image.open(io.BytesIO(requests.get(c["small"], timeout=20).content)).convert("RGB")
                im.thumbnail((320, 320))
                buf = io.BytesIO(); im.save(buf, "JPEG", quality=75)
                content += [{"type": "text", "text": f"{key}{i}"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(buf.getvalue()).decode()}}]
            except Exception:
                pass
    if not content:
        return None, None
    brief = json.dumps({k: d.get(k) for k in ("category", "kicker", "headline", "what")}, ensure_ascii=False)
    content.append({"type": "text", "text": f"""매거진형 뉴스 카드의 사진 편집자로서 고르세요. 기사: {brief}
C번호=표지(세로) 후보, B번호=본문 가로 사진 후보.
기준: 이 기사의 상황·장면을 실제로 보여주거나 독자가 바로 연결할 수 있는 사진. 단어만 같고 상황이 다른 사진(예: 원격수술 기사에 게임기, 동물 실험 기사에 사람 환자)은 안 됨.
기사 주제와 관계없는 상표가 크게 보이면 피하고, 기사 주제가 그 제품·브랜드라면 괜찮음. 표지와 본문은 서로 다른 사진.
맞는 사진이 없으면 -1 (억지로 고르지 말 것 — 사진 없이 어두운 배경이 낫다).
JSON만: {{"cover": 0, "body": 2, "why": "한 줄"}}"""})
    msg = Anthropic().messages.create(model=REVIEW_MODEL, max_tokens=6000, messages=[{"role": "user", "content": content}])
    log_cost(msg, "photo_pick")
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    pick = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    print("사진 선택:", pick)

    def load(lst, i):
        if i is None or int(i) < 0 or int(i) >= len(lst):
            return None
        c = lst[int(i)]
        img = Image.open(io.BytesIO(requests.get(c["large"], timeout=30).content)).convert("RGB")
        return img, c["credit"], c["id"]
    return (load(sets.get("C", []), pick.get("cover")) if need_cover else None,
            load(sets.get("B", []), pick.get("body")) if need_body else None)


def get_photo(query, orientation="portrait", skip_ids=(), alt=None):
    """Pixabay → Pexels 순서로 시도. 안 맞으면 다른 검색어(alt)로 한 번 더, 그래도 없으면 None(어두운 배경)"""
    for q in [query] + ([alt] if alt and alt != query else []):
        ph = pixabay_photo(q, orientation, skip_ids) or pexels_photo(q, orientation, skip_ids)
        if ph:
            return ph
    return None


def pexels_photo(query, orientation="portrait", skip_ids=()):
    """Pexels 무료 사진(상업적 이용 가능, 출처 표기 권장). 실패하면 None.
    반환: (PIL 이미지, 사진작가 이름, 사진 id)"""
    if not PEXELS_KEY or not query:
        return None
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         params={"query": query, "orientation": orientation, "per_page": 10},
                         headers={"Authorization": PEXELS_KEY}, timeout=20).json()
        photos = [(photo_fit(query, (p.get("alt") or "").replace(" ", ",")), p) for p in (r.get("photos") or []) if p["id"] not in skip_ids]
        photos = [x for x in photos if x[0] > 0]
        if not photos:
            return None
        best = max(x[0] for x in photos)
        p = random.choice([x[1] for x in photos if x[0] == best][:3])
        img = Image.open(io.BytesIO(requests.get(p["src"]["large2x"], timeout=30).content)).convert("RGB")
        return img, f"{p.get('photographer', '')} / Pexels", p["id"]
    except Exception as e:
        print("사진 가져오기 실패, 글자 디자인으로 대체:", e)
        return None


def cover_crop(ph, w, h):
    sc = max(w / ph.width, h / ph.height)
    ph = ph.resize((int(ph.width * sc) + 1, int(ph.height * sc) + 1), Image.LANCZOS)
    x, y = (ph.width - w) // 2, (ph.height - h) // 2
    return ph.crop((x, y, x + w, y + h))


# ---------- 그리기 도우미 ----------
def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def vgradient(top, bottom, size=(W, H)):
    t, b = hex2rgb(top), hex2rgb(bottom)
    col = Image.new("RGB", (1, size[1]))
    for y in range(size[1]):
        k = y / (size[1] - 1)
        col.putpixel((0, y), tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3)))
    return col.resize(size)


def glow(img, center, radius, color, alpha=170):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=hex2rgb(color) + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    img.alpha_composite(layer)


def paste_icon(img, icon, center, size, angle=0, shadow=True):
    ic = icon.resize((size, size), Image.LANCZOS)
    if angle:
        ic = ic.rotate(angle, resample=Image.BICUBIC, expand=True)
    x, y = center[0] - ic.width // 2, center[1] - ic.height // 2
    if shadow:
        sh = Image.new("RGBA", ic.size, (0, 0, 0, 0))
        sh.putalpha(ic.getchannel("A").point(lambda a: int(a * 0.45)))
        sh = sh.filter(ImageFilter.GaussianBlur(22))
        img.alpha_composite(sh, (x + 18, y + 34))
    img.alpha_composite(ic, (x, y))


def wrap(text, f, draw, max_w):
    """어절 단위 줄바꿈(한 어절이 너무 길면 글자 단위)"""
    lines, line = [], ""
    for word in text.split(" "):
        test = (line + " " + word).strip()
        if draw.textlength(test, font=f) <= max_w:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
            while draw.textlength(line, font=f) > max_w:
                cut = len(line)
                while draw.textlength(line[:cut], font=f) > max_w:
                    cut -= 1
                lines.append(line[:cut])
                line = line[cut:]
    if line:
        lines.append(line)
    return lines


def draw_highlight_line(dr, x, y, line, hl, f, fg, accent):
    """줄 안의 강조 단어만 강조색으로"""
    if hl and hl in line:
        a, b = line.split(hl, 1)
        dr.text((x, y), a, font=f, fill=fg)
        x += dr.textlength(a, font=f)
        dr.text((x, y), hl, font=f, fill=accent)
        x += dr.textlength(hl, font=f)
        dr.text((x, y), b, font=f, fill=fg)
    else:
        dr.text((x, y), line, font=f, fill=fg)


# ---------- 카드 렌더링 (에디토리얼 스타일) ----------
SERIF = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"
SERIF_BLACK = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Black.ttc"
if not os.path.exists(SERIF_BLACK):
    SERIF_BLACK = SERIF
SANS_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
SANS_R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
CAT_EN = {"경제": "ECONOMY", "사회": "SOCIETY", "IT": "TECH", "생활": "LIFE",
          "국제": "WORLD", "스포츠": "SPORTS", "연예": "CULTURE"}
# (배경, 본문, 보조, 포인트)
# 데일리찝 단일 브랜드 테마: 블랙 + 찝 옐로 (피드 통일)
ED_THEMES = [
    ("#111111", "#F3F0EA", "#8A8780", "#FFE000"),
]


def tracked(dr, xy, text, f, fill, spacing=4):
    x, y = xy
    for ch in text:
        dr.text((x, y), ch, font=f, fill=fill)
        x += dr.textlength(ch, font=f) + spacing
    return x


def fit_font(path, text, max_w, start, minimum, dr):
    size = start
    while size > minimum:
        f = font(path, size)
        if dr.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return font(path, minimum)


def render_card(d, path, theme_idx=0, photo=None, cover=False):
    bg, fg, sub, accent = ED_THEMES[theme_idx % len(ED_THEMES)]
    img = Image.new("RGB", (W, H), bg)
    dr = ImageDraw.Draw(img)
    M = 72
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    issue_no = d.get("issue_no", 1)

    # 마스트헤드
    f_mast = font(SANS_B, 26)
    tracked(dr, (M, 62), "DAILY JJIP", f_mast, fg, 6)
    right = f"No.{issue_no:03d}   {today.strftime('%Y.%m.%d')}"
    dr.text((W - M - dr.textlength(right, font=font(SANS_R, 24)), 64), right, font=font(SANS_R, 24), fill=sub)
    dr.line((M, 112, W - M, 112), fill=fg, width=3)

    # 카테고리 키커
    f_cat = font(SANS_B, 24)
    x = tracked(dr, (M, 140), CAT_EN.get(d["category"], "ISSUE"), f_cat, accent, 5)
    dr.text((x + 14, 140), "— " + d.get("kicker", d["category"]), font=font(SANS_R, 24), fill=sub)

    y = 210
    if photo is not None:
        # 흑백 고대비 사진 블록
        ph = photo.convert("L")
        ph = Image.eval(ph, lambda v: max(0, min(255, int((v - 128) * 1.25 + 128))))
        bw, bh = W - 2 * M, 420
        sc = max(bw / ph.width, bh / ph.height)
        ph = ph.resize((int(ph.width * sc), int(ph.height * sc)), Image.LANCZOS)
        ph = ph.crop(((ph.width - bw) // 2, (ph.height - bh) // 2, (ph.width - bw) // 2 + bw, (ph.height - bh) // 2 + bh))
        img.paste(ph.convert("RGB"), (M, y))
        dr = ImageDraw.Draw(img)
        y += bh + 40
        head_size = 92
    else:
        # 대형 숫자/키워드 타이포
        num = d.get("big_number", "")
        f_num = fit_font(SERIF_BLACK, num, W - 2 * M, 300, 120, dr)
        bb = dr.textbbox((0, 0), num, font=f_num)
        dr.text((M - bb[0], y - bb[1]), num, font=f_num, fill=accent)
        y += (bb[3] - bb[1]) + 26
        dr.text((M, y), d.get("big_label", ""), font=font(SANS_B, 34), fill=fg)
        y += 80
        head_size = 100
    if cover:
        head_size = 124 if photo is None else 104
        y += 20

    # 헤드라인 (세리프)
    f_head = font(SERIF_BLACK, head_size)
    for ln in wrap(d["headline"], f_head, dr, W - 2 * M)[:2]:
        dr.text((M, y), ln, font=f_head, fill=fg)
        y += int(head_size * 1.22)
    y += 18
    if cover:
        dr.line((M, H - 96, W - M, H - 96), fill=fg, width=2)
        dr.text((M, H - 74), "오늘의 이슈를 찝어드립니다", font=font(SANS_R, 22), fill=sub)
        h = "@daily.jjip"
        dr.text((W - M - dr.textlength(h, font=font(SANS_B, 24)), H - 76), h, font=font(SANS_B, 24), fill=fg)
        img.save(path, "JPEG", quality=94)
        return

    # 포인트: 번호 + 얇은 구분선
    f_pt = font(SANS_R, 34)
    f_no = font(SANS_B, 26)
    for i, p in enumerate(d["points"][:3], 1):
        dr.line((M, y, W - M, y), fill=sub, width=1)
        y += 22
        dr.text((M, y + 4), f"0{i}", font=f_no, fill=accent)
        dr.text((M + 70, y), wrap(p, f_pt, dr, W - 2 * M - 70)[0], font=f_pt, fill=fg)
        y += 66
    dr.line((M, y, W - M, y), fill=sub, width=1)

    # 에디터 코멘트
    f_one = font(SERIF, 34)
    one = wrap(d["oneliner"], f_one, dr, W - 2 * M - 40)[0]
    dr.text((M, H - 176), "“", font=font(SERIF_BLACK, 60), fill=accent)
    dr.text((M + 40, H - 168), one, font=f_one, fill=fg)

    # 푸터
    dr.line((M, H - 96, W - M, H - 96), fill=fg, width=2)
    credit = "출처: 언론 보도 종합" + (" · Photo: Pexels" if photo is not None else "")
    dr.text((M, H - 74), credit, font=font(SANS_R, 22), fill=sub)
    h = "@daily.jjip"
    dr.text((W - M - dr.textlength(h, font=font(SANS_B, 24)), H - 76), h, font=font(SANS_B, 24), fill=fg)

    img.save(path, "JPEG", quality=94)


def render_photo_cover(d, path, theme_idx, photo, credit):
    """실사 표지: 사진을 꽉 채우고 아래쪽을 어둡게 깔아 큰 제목을 얹는다."""
    _, _, _, accent = ED_THEMES[theme_idx % len(ED_THEMES)]
    if photo is not None:
        img = cover_crop(photo, W, H).convert("RGBA")
    else:
        # 사진을 못 구한 경우에도 같은 레이아웃: 어두운 배경 + 은은한 빛
        img = vgradient("#2A2A2A", "#0A0A0A").convert("RGBA")
        glow(img, (W - 180, 260), 380, accent, 45)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for y in range(H):
        top = max(0, 1 - y / 260) * 150                 # 위쪽 살짝
        bottom = max(0, (y - H * 0.35) / (H * 0.65)) * 235  # 아래쪽 진하게
        sd.line((0, y, W, y), fill=(0, 0, 0, int(min(240, top + bottom))))
    img.alpha_composite(shade)
    img = img.convert("RGB")
    dr = ImageDraw.Draw(img)
    M, white = 72, "#FFFFFF"
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    tracked(dr, (M, 62), "DAILY JJIP", font(SANS_B, 26), white, 6)
    right = f"No.{d.get('issue_no', 1):03d}   {today.strftime('%Y.%m.%d')}"
    dr.text((W - M - dr.textlength(right, font=font(SANS_R, 24)), 64), right, font=font(SANS_R, 24), fill=white)
    dr.line((M, 112, W - M, 112), fill=white, width=3)
    x = tracked(dr, (M, 140), CAT_EN.get(d["category"], "ISSUE"), font(SANS_B, 24), accent, 5)
    dr.text((x + 14, 140), "— " + d.get("kicker", ""), font=font(SANS_R, 24), fill=white)

    # 아래에서 위로 쌓기: 제목 → 숫자
    # 제목이 길면 잘라내지 않고 글자를 줄여 2줄(최소 크기에선 3줄) 안에 전부 넣는다
    for size in (112, 104, 96, 88, 80, 72):
        f_head = font(SERIF_BLACK, size)
        lines = wrap(d["headline"], f_head, dr, W - 2 * M)
        if len(lines) <= 2:
            break
    lh = int(size * 1.21)
    y = H - 150 - len(lines) * lh
    num = d.get("big_number", "")
    if num:
        f_num = fit_font(SERIF_BLACK, num, W - 2 * M, 150, 80, dr)
        bb = dr.textbbox((0, 0), num, font=f_num)
        ny = y - (bb[3] - bb[1]) - 70
        dr.text((M - bb[0], ny - bb[1]), num, font=f_num, fill=accent)
        dr.text((M, ny + (bb[3] - bb[1]) + 14), d.get("big_label", ""), font=font(SANS_B, 32), fill=white)
    for ln in lines:
        dr.text((M, y), ln, font=f_head, fill=white)
        y += lh
    dr.line((M, H - 96, W - M, H - 96), fill=white, width=2)
    if credit:
        dr.text((M, H - 74), f"Photo: {credit}", font=font(SANS_R, 22), fill="#CCCCCC")
    t = "SWIPE  →"
    tracked(dr, (W - M - dr.textlength(t, font=font(SANS_B, 24)) - 30, H - 76), t, font(SANS_B, 24), accent, 3)
    img.save(path, "JPEG", quality=94)


def render_follow(d, path, theme_idx):
    """마지막 장: 팔로우 요청"""
    bg, fg, sub, accent = ED_THEMES[theme_idx % len(ED_THEMES)]
    img = Image.new("RGB", (W, H), bg)
    dr = ImageDraw.Draw(img)
    M = 72
    tracked(dr, (M, 62), "DAILY JJIP", font(SANS_B, 26), fg, 6)
    dr.line((M, 112, W - M, 112), fill=fg, width=3)
    # 큰 문구
    y = 330
    kick, lines = "EVERY DAY", ["세상 돌아가는 일,", "딱 찝어서."]
    tracked(dr, (M, y), kick, font(SANS_B, 26), accent, 6)
    y += 70
    size = 150
    while size > 90 and max(dr.textlength(l, font=font(SERIF_BLACK, size)) for l in lines) > W - 2 * M:
        size -= 6
    for ln in lines:
        dr.text((M, y), ln, font=font(SERIF_BLACK, size), fill=fg)
        y += int(size * 1.2)
    y += 40
    dr.text((M, y), "오늘 꼭 알아야 할 이슈만 골라 정리합니다.", font=font(SANS_R, 36), fill=fg)
    y += 56
    dr.text((M, y), "매일 아침부터 저녁까지 업데이트", font=font(SANS_R, 36), fill=sub)
    y += 110
    # 팔로우 버튼
    dr.rounded_rectangle((M, y, W - M, y + 120), radius=60, fill=accent)
    t = "+ 팔로우  @daily.jjip"
    f = font(SANS_B, 46)
    dr.text(((W - dr.textlength(t, font=f)) / 2, y + 28), t, font=f, fill=bg)
    y += 170
    t2 = "저장해두고 다시 보기  ·  친구에게 공유하기"
    dr.text(((W - dr.textlength(t2, font=font(SANS_R, 30))) / 2, y), t2, font=font(SANS_R, 30), fill=sub)
    dr.line((M, H - 96, W - M, H - 96), fill=fg, width=2)
    h = "@daily.jjip"
    dr.text((W - M - dr.textlength(h, font=font(SANS_B, 24)), H - 76), h, font=font(SANS_B, 24), fill=fg)
    img.save(path, "JPEG", quality=94)


# ---------- 캐러셀 슬라이드 ----------
def frame(d, theme_idx, page, total, section_en, section_ko):
    """공통 틀: 마스트헤드 + 섹션 제목 + 푸터. (img, dr, y시작, 색상) 반환"""
    bg, fg, sub, accent = ED_THEMES[theme_idx % len(ED_THEMES)]
    img = Image.new("RGB", (W, H), bg)
    dr = ImageDraw.Draw(img)
    M = 72
    tracked(dr, (M, 62), "DAILY JJIP", font(SANS_B, 26), fg, 6)
    pg = f"{page:02d} / {total:02d}"
    dr.text((W - M - dr.textlength(pg, font=font(SANS_R, 24)), 64), pg, font=font(SANS_R, 24), fill=sub)
    dr.line((M, 112, W - M, 112), fill=fg, width=3)
    x = tracked(dr, (M, 140), section_en, font(SANS_B, 24), accent, 5)
    dr.text((x + 14, 140), "— " + d["headline"], font=font(SANS_R, 24), fill=sub)
    dr.text((M, 200), section_ko, font=font(SERIF_BLACK, 84), fill=fg)
    dr.line((W - M - 40, H - 96, W - M, H - 96), fill=fg, width=2)
    dr.line((M, H - 96, W - M, H - 96), fill=fg, width=2)
    dr.text((M, H - 74), "출처: " + ", ".join(d.get("sources", [])[:3] or ["언론 보도 종합"]), font=font(SANS_R, 22), fill=sub)
    h = "@daily.jjip"
    dr.text((W - M - dr.textlength(h, font=font(SANS_B, 24)), H - 76), h, font=font(SANS_B, 24), fill=fg)
    return img, dr, 380, (bg, fg, sub, accent), M


def render_slides(d, outdir, theme_idx=0, photo=None, photo2=None):
    os.makedirs(outdir, exist_ok=True)
    total = 6
    paths = []

    # 1. 표지
    p = os.path.join(outdir, "01.jpg")
    if True:  # 표지는 항상 같은 레이아웃
        render_photo_cover(d, p, theme_idx, photo[0] if photo else None, photo[1] if photo else "")
    else:
        render_card(d, p, theme_idx, None, cover=True)
        img = Image.open(p)
        dr = ImageDraw.Draw(img)
        accent = ED_THEMES[theme_idx % len(ED_THEMES)][3]
        t, f = "SWIPE  →", font(SANS_B, 24)
        tracked(dr, (W - 72 - dr.textlength(t, font=f) - 30, 142), t, f, accent, 3)
        img.save(p, "JPEG", quality=94)
    paths.append(p)

    # 2. 무슨 일이야 (사진이 있으면 위쪽에 사진 띠)
    img, dr, y, (bg, fg, sub, accent), M = frame(d, theme_idx, 2, total, "WHAT HAPPENED", "무슨 일이야?")
    if photo2 is not None:
        band = cover_crop(photo2[0], W - 2 * M, 400)
        img.paste(band, (M, 330))
        dr = ImageDraw.Draw(img)
        dr.rectangle((M, 330, W - M, 730), fill=None)
        cap = f"Photo: {photo2[1]}"
        dr.text((M, 740), cap, font=font(SANS_R, 20), fill=sub)
        y, f_b, lh, gap = 800, font(SANS_R, 40), 58, 30
    else:
        f_b, lh, gap = font(SANS_R, 50), 74, 70
    for i, sent in enumerate(d.get("what", [])[:3]):
        dr.rectangle((M, y + lh // 5, M + 8, y + lh - 14), fill=accent if i == 0 else sub)
        for ln in wrap(sent, f_b, dr, W - 2 * M - 36)[:2]:
            dr.text((M + 36, y), ln, font=f_b, fill=fg)
            y += lh
        y += gap
    p = os.path.join(outdir, "02.jpg"); img.save(p, "JPEG", quality=94); paths.append(p)

    # 3. 세부 내용 (숫자 3개 + 소제목 3개)
    img, dr, y, (bg, fg, sub, accent), M = frame(d, theme_idx, 3, total, "DETAILS", "세부 내용")
    nums = [n for n in d.get("numbers", []) if isinstance(n, dict)][:3]
    if nums:
        col = (W - 2 * M) // len(nums)
        for i, n in enumerate(nums):
            x0 = M + i * col
            f_v = fit_font(SERIF_BLACK, n.get("value", ""), col - 48, 96, 40, dr)
            dr.text((x0, y), n.get("value", ""), font=f_v, fill=accent)
            dr.text((x0, y + 118), wrap(n.get("label", ""), font(SANS_R, 26), dr, col - 24)[0],
                    font=font(SANS_R, 26), fill=sub)
        y += 190
        dr.line((M, y, W - M, y), fill=sub, width=1)
        y += 40
    for det in [x for x in d.get("details", []) if isinstance(x, dict)][:3]:
        dr.rectangle((M, y + 10, M + 10, y + 52), fill=accent)
        dr.text((M + 34, y), det.get("head", ""), font=font(SANS_B, 44), fill=fg)
        y += 70
        for ln in wrap(det.get("body", ""), font(SANS_R, 36), dr, W - 2 * M - 34)[:2]:
            dr.text((M + 34, y), ln, font=font(SANS_R, 36), fill=sub)
            y += 52
        y += 40
    p = os.path.join(outdir, "03.jpg"); img.save(p, "JPEG", quality=94); paths.append(p)

    # 4. 요약 + 앞으로 주목할 포인트
    img, dr, y, (bg, fg, sub, accent), M = frame(d, theme_idx, 4, total, "SUMMARY & NEXT", "정리하면")
    tracked(dr, (M, y), "SUMMARY", font(SANS_B, 24), accent, 5)
    y += 50
    for i, pt in enumerate(d.get("points", [])[:3], 1):
        dr.text((M, y - 6), f"0{i}", font=font(SERIF_BLACK, 56), fill=accent)
        dr.text((M + 96, y), wrap(pt, font(SANS_B, 42), dr, W - 2 * M - 96)[0], font=font(SANS_B, 42), fill=fg)
        y += 84
    y += 20
    dr.line((M, y, W - M, y), fill=sub, width=1)
    y += 40
    tracked(dr, (M, y), "WATCH NEXT  —  앞으로 주목할 포인트", font(SANS_B, 24), accent, 3)
    y += 60
    for w in [x for x in d.get("watch", []) if isinstance(x, dict)][:2]:
        dr.text((M, y), "→  " + w.get("head", ""), font=font(SANS_B, 42), fill=fg)
        y += 64
        for ln in wrap(w.get("body", ""), font(SANS_R, 34), dr, W - 2 * M - 56)[:2]:
            dr.text((M + 56, y), ln, font=font(SANS_R, 34), fill=sub)
            y += 50
        y += 36
    p = os.path.join(outdir, "04.jpg"); img.save(p, "JPEG", quality=94); paths.append(p)

    # 6. 에디터 코멘트 + 댓글 토론 질문
    img, dr, y, (bg, fg, sub, accent), M = frame(d, theme_idx, 5, total, "EDITOR'S NOTE", "에디터의 한 줄")
    dr.text((M, y - 10), "“", font=font(SERIF_BLACK, 140), fill=accent)
    y += 120
    f_q = font(SERIF_BLACK, 58)
    for ln in wrap(d.get("oneliner", ""), f_q, dr, W - 2 * M)[:2]:
        dr.text((M, y), ln, font=f_q, fill=fg)
        y += 80
    deb = d.get("debate") or {}
    if isinstance(deb, dict) and deb.get("question"):
        by = 780
        dr.line((M, by, W - M, by), fill=sub, width=1)
        tracked(dr, (M, by + 34), "TALK  —  여러분 생각은?", font(SANS_B, 26), accent, 3)
        qy = by + 84
        for ln in wrap(deb["question"], font(SANS_B, 46), dr, W - 2 * M)[:2]:
            dr.text((M, qy), ln, font=font(SANS_B, 46), fill=fg)
            qy += 64
        qy += 20
        half = (W - 2 * M - 24) // 2
        for i, (lab, txt) in enumerate((("A", deb.get("a", "")), ("B", deb.get("b", "")))):
            x0 = M + i * (half + 24)
            dr.rounded_rectangle((x0, qy, x0 + half, qy + 96), radius=48,
                                 outline=accent if i == 0 else fg, width=3)
            t = f"{lab}. {txt}"
            f = fit_font(SANS_B, t, half - 40, 34, 22, dr)
            dr.text((x0 + (half - dr.textlength(t, font=f)) / 2, qy + 28), t, font=f, fill=fg)
        qy += 124
        t = "댓글로 A 또는 B를 남겨주세요"
        dr.text(((W - dr.textlength(t, font=font(SANS_R, 30))) / 2, qy), t, font=font(SANS_R, 30), fill=sub)
    p = os.path.join(outdir, "05.jpg"); img.save(p, "JPEG", quality=94); paths.append(p)

    # 6. 팔로우 요청
    p = os.path.join(outdir, "06.jpg")
    render_follow(d, p, theme_idx)
    paths.append(p)
    return paths


# ---------- 인스타 게시 ----------
def make_reel(slide_paths, out_path, sec=2.2):
    """카드 6장을 9:16 세로 영상(릴스)으로: 검정 배경 가운데 카드, 장마다 sec초 + 짧은 페이드"""
    import subprocess, tempfile
    tmp = tempfile.mkdtemp()
    frames = []
    for i, p in enumerate(slide_paths):
        bg = Image.new("RGB", (1080, 1920), ED_THEMES[0][0])
        card = Image.open(p).convert("RGB")
        bg.paste(card, (0, (1920 - 1350) // 2))
        dr = ImageDraw.Draw(bg)
        if i == 0:
            hint = "끝까지 보면 정리 끝 →"
            dr.text((72, 1920 - 230), hint, font=font(SANS_B, 38), fill=ED_THEMES[0][3])
        h = "@daily.jjip"
        dr.text((1080 - 72 - dr.textlength(h, font=font(SANS_B, 34)), 1920 - 230), h, font=font(SANS_B, 34), fill=ED_THEMES[0][1])
        f = os.path.join(tmp, f"{i:02d}.png")
        bg.save(f)
        frames.append(f)
    n = len(frames)
    inputs = []
    for f in frames:
        inputs += ["-loop", "1", "-t", str(sec + 0.4), "-i", f]
    # 장 사이 0.4초 페이드로 이어붙이기
    filt, last = [], "[0:v]"
    for i in range(1, n):
        off = round(i * sec, 2)
        out = f"[v{i}]"
        filt.append(f"{last}[{i}:v]xfade=transition=fade:duration=0.4:offset={off}{out}")
        last = out
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-f", "lavfi", "-t", str(n * sec + 0.4),
           "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
           "-filter_complex", ";".join(filt) + f";{last}format=yuv420p[vout]",
           "-map", "[vout]", "-map", f"{n}:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-r", "30", "-c:a", "aac", "-shortest", "-movflags", "+faststart", out_path]
    subprocess.run(cmd, check=True)
    return out_path


LOCATION_ID = os.getenv("IG_LOCATION_ID", "").strip()  # 위치 태그(페이스북 장소 페이지 ID). 비어 있으면 태그 없이 게시


def create_container(base, data):
    """미디어 컨테이너 생성. 위치 태그로 오류가 나면 위치 없이 한 번 더 시도(게시는 반드시 진행)"""
    if LOCATION_ID:
        r = requests.post(f"{base}/media", data={**data, "location_id": LOCATION_ID}).json()
        if "id" in r:
            return r
        health_event("location_fail", r)
        print("위치 태그 실패 → 위치 없이 진행:", r)
    return requests.post(f"{base}/media", data=data).json()


FB_TOKEN = os.getenv("FB_ACCESS_TOKEN", "").strip()  # 페이스북 로그인 토큰(있을 때만 인스타 추천 음악 사용)
FB_API = "https://graph.facebook.com/v22.0"


def fb_ig_user_id():
    """페이스북 로그인 쪽 인스타 비즈니스 계정 ID (연결된 페이지에서 자동 조회)"""
    if os.getenv("FB_IG_USER_ID"):
        return os.getenv("FB_IG_USER_ID")
    r = requests.get(f"{FB_API}/me/accounts", params={"fields": "instagram_business_account",
                                                       "access_token": FB_TOKEN}, timeout=30).json()
    for pg in r.get("data", []):
        if pg.get("instagram_business_account"):
            return pg["instagram_business_account"]["id"]
    raise RuntimeError(f"페이스북 페이지에 연결된 인스타 계정을 못 찾음: {r}")


def pick_trending_audio(uid):
    """인스타 추천(트렌딩) 음악 중 최근 14일간 안 쓴 곡 하나. 3자 사용 허가된 음악만 반환됨"""
    r = requests.get(f"{FB_API}/ig_audio", params={"audio_type": "music", "user_id": uid,
                                                    "access_token": FB_TOKEN}, timeout=30).json()
    used = set()
    if os.path.exists("reels_history.json"):
        cut = (datetime.datetime.utcnow() - datetime.timedelta(days=14)).strftime("%Y%m%d_%H%M")
        used = {x.get("audio_id") for x in json.load(open("reels_history.json", encoding="utf-8")) if x.get("date", "") >= cut}
    for a in r.get("data", []):
        if a.get("id") and a["id"] not in used:
            return a
    raise RuntimeError(f"추천 음악 없음: {str(r)[:200]}")


def ig_publish_reel_music(video_url, caption, cover_url=None):
    """추천 음악을 붙여 릴스 게시(페이스북 로그인 API). 반환: (게시 ID, 음악 정보)"""
    uid = fb_ig_user_id()
    audio = pick_trending_audio(uid)
    data = {"media_type": "REELS", "video_url": video_url, "caption": caption, "share_to_feed": "false",
            "audio_configuration": json.dumps({"audio_id": audio["id"], "audio_volume": 100, "video_volume": 1}),
            "access_token": FB_TOKEN}
    if cover_url:
        data["cover_url"] = cover_url
    if LOCATION_ID:
        data["location_id"] = LOCATION_ID
    r = requests.post(f"{FB_API}/{uid}/media", data=data).json()
    if "id" not in r and LOCATION_ID:
        health_event("location_fail", r)
        data.pop("location_id")
        r = requests.post(f"{FB_API}/{uid}/media", data=data).json()
    if "id" not in r:
        raise RuntimeError(f"음악 릴스 컨테이너 실패: {r}")
    for _ in range(40):
        time.sleep(10)
        st = requests.get(f"{FB_API}/{r['id']}", params={"fields": "status_code,status", "access_token": FB_TOKEN}).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"음악 릴스 처리 실패: {st}")
    r2 = requests.post(f"{FB_API}/{uid}/media_publish", data={"creation_id": r["id"], "access_token": FB_TOKEN}).json()
    if "id" not in r2:
        raise RuntimeError(f"음악 릴스 게시 실패: {r2}")
    return r2["id"], audio


def ig_publish_reel(video_url, caption, cover_url=None):
    """릴스 게시. 피드 그리드는 캐러셀로 통일하기 위해 share_to_feed=false (릴스 탭·추천에만 노출)"""
    base = f"https://graph.instagram.com/v21.0/{IG_USER_ID or 'me'}"
    data = {"media_type": "REELS", "video_url": video_url, "caption": caption,
            "share_to_feed": "false", "access_token": IG_TOKEN}
    if cover_url:
        data["cover_url"] = cover_url
    r = create_container(base, data)
    if "id" not in r:
        raise RuntimeError(f"릴스 컨테이너 실패: {r}")
    cid = r["id"]
    for _ in range(40):  # 영상 처리 대기 (최대 약 6~7분)
        time.sleep(10)
        st = requests.get(f"https://graph.instagram.com/v21.0/{cid}",
                          params={"fields": "status_code,status", "access_token": IG_TOKEN}).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"릴스 처리 실패: {st}")
    r2 = requests.post(f"{base}/media_publish", data={"creation_id": cid, "access_token": IG_TOKEN}).json()
    if "id" not in r2:
        raise RuntimeError(f"릴스 게시 실패: {r2}")
    return r2["id"]


def ig_publish(image_urls, caption):
    """이미지 여러 장을 캐러셀(넘겨보기) 게시물로 올린다."""
    base = f"https://graph.instagram.com/v21.0/{IG_USER_ID or 'me'}"
    children = []
    for u in image_urls:
        r = requests.post(f"{base}/media", data={
            "image_url": u, "is_carousel_item": "true", "access_token": IG_TOKEN}).json()
        if "id" not in r:
            raise RuntimeError(f"슬라이드 업로드 실패: {u} {r}")
        children.append(r["id"])
    r = create_container(base, {
        "media_type": "CAROUSEL", "children": ",".join(children),
        "caption": caption, "access_token": IG_TOKEN})
    if "id" not in r:
        raise RuntimeError(f"캐러셀 생성 실패: {r}")
    time.sleep(20)  # 인스타 쪽 처리 대기
    r2 = requests.post(f"{base}/media_publish", data={
        "creation_id": r["id"], "access_token": IG_TOKEN}).json()
    if "id" not in r2:
        # 인스타가 오류를 돌려줘도 실제로는 올라간 경우가 있음 → 최근 게시물로 확인
        time.sleep(15)
        latest = requests.get(f"https://graph.instagram.com/v21.0/me/media",
                              params={"fields": "id,timestamp", "limit": 1, "access_token": IG_TOKEN}).json()
        try:
            m = latest["data"][0]
            ts = datetime.datetime.strptime(m["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")
            if (datetime.datetime.utcnow() - ts).total_seconds() < 600:
                print("게시 응답은 오류였지만 실제로 게시됨:", m["id"], r2)
                health_event("blocked_warning", r2)
                return m["id"]
        except Exception:
            pass
        raise RuntimeError(f"게시 실패: {r2}")
    return r2["id"]


HEALTH = "health.json"
COSTS = "costs.json"
KRW_PER_USD = 1450
MONTHLY_BUDGET_KRW = int(os.getenv("MONTHLY_BUDGET_KRW", "45000"))  # 30일 예산 5만원 중 안전 여유 5천원
# 추정 단가(USD): 실제 청구액은 Console 기준. 주간 보고에서 차이를 점검.
PRICE_IN, PRICE_OUT, PRICE_SEARCH = 3 / 1e6, 15 / 1e6, 10 / 1000


def log_cost(msg, purpose):
    try:
        u = msg.usage
        searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
        usd = (u.input_tokens * PRICE_IN + u.output_tokens * PRICE_OUT + searches * PRICE_SEARCH
               + (getattr(u, "cache_read_input_tokens", 0) or 0) * PRICE_IN * 0.1)
        items = json.load(open(COSTS, encoding="utf-8")) if os.path.exists(COSTS) else []
        items.append({"at": datetime.datetime.utcnow().isoformat(timespec="seconds"), "purpose": purpose,
                      "usd": round(usd, 4), "krw": round(usd * KRW_PER_USD), "in": u.input_tokens,
                      "out": u.output_tokens, "search": searches})
        json.dump(items[-2000:], open(COSTS, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    except Exception as e:
        print("비용 기록 실패:", e)


def spend_30d_krw():
    if not os.path.exists(COSTS):
        return 0
    cut = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat()
    return sum(x["krw"] for x in json.load(open(COSTS, encoding="utf-8")) if x["at"] >= cut)
DAILY_CAP = int(os.getenv("DAILY_CAP", "0")) or (6 if (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y%m%d") <= "20261025" else 3)  # 하루 최대 생성 수 (집중 기간 6, 이후 3)


def load_health():
    return json.load(open(HEALTH, encoding="utf-8")) if os.path.exists(HEALTH) else {"events": []}


def health_event(kind, detail=""):
    h = load_health()
    h["events"] = (h.get("events", []) + [{"at": datetime.datetime.utcnow().isoformat(timespec="seconds"),
                                           "kind": kind, "detail": str(detail)[:300]}])[-200:]
    json.dump(h, open(HEALTH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return h


REEL_DAILY_CAP = int(os.getenv("REEL_DAILY_CAP", "1"))  # 전용 릴스 하루 최대 1건


def guard_ok(kind="carousel"):
    """비용이 드는 생성 전에 안전장치 확인: 예산 / 경고 휴식 / 일시정지 / 하루 생성 한도(종류별)"""
    h = load_health()
    spent = spend_30d_krw()
    if spent >= MONTHLY_BUDGET_KRW:
        print(f"[안전장치] 최근 30일 추정 지출 {spent:,}원 ≥ 예산 {MONTHLY_BUDGET_KRW:,}원 → 생성 중단")
        health_event("skip_budget", spent)
        return False
    since = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat()
    warns = [x for x in h.get("events", []) if x["kind"] == "blocked_warning" and x["at"] >= since]
    if len(warns) >= 2 and not h.get("paused_until"):
        # 계정 보호 최우선: 24시간 내 인스타 활동 제한 경고 2회 → 12시간 자동 휴식
        h["paused_until"] = (datetime.datetime.utcnow() + datetime.timedelta(hours=12)).isoformat(timespec="seconds")
        h["pause_reason"] = f"24시간 내 인스타 활동 제한 경고 {len(warns)}회 → 계정 보호 휴식"
        json.dump(h, open(HEALTH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    pu = h.get("paused_until")
    if pu and datetime.datetime.utcnow().isoformat() < pu:
        print(f"[안전장치] 일시정지 중 ({h.get('pause_reason')}) → {pu}까지 생성 건너뜀")
        health_event("skip_paused", h.get("pause_reason"))
        return False
    kst_today = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y%m%d")
    n = 0
    for t in (json.load(open(TOPICS, encoding="utf-8")) if os.path.exists(TOPICS) else []):
        try:
            dt = datetime.datetime.strptime(t["date"], "%Y%m%d_%H%M") + datetime.timedelta(hours=9)
            if t.get("kind", "carousel") == kind and dt.strftime("%Y%m%d") == kst_today:
                n += 1
        except Exception:
            pass
    cap = REEL_DAILY_CAP if kind == "reel" else DAILY_CAP
    if n >= cap:
        print(f"[안전장치] 오늘 {kind} 이미 {n}건 생성 → 한도 {cap}건 도달, 건너뜀")
        health_event("skip_cap", n)
        return False
    return True


REVIEW_MODEL = os.getenv("REVIEW_MODEL", "claude-haiku-5-5")


def review_post(d, out_dir):
    """게시 전 최종 검수: AI가 완성된 표지(01)·본문 사진(02) 이미지와 문구를 직접 보고 판정.
    반환 {"cover_ok","body_ok","text_ok","issues":[...]}"""
    import base64
    from anthropic import Anthropic
    imgs = []
    for name in ("01.jpg", "02.jpg", "03.jpg", "04.jpg", "05.jpg"):
        im = Image.open(os.path.join(out_dir, name)).convert("RGB")
        im.thumbnail((720, 900))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=80)
        imgs.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                  "data": base64.b64encode(buf.getvalue()).decode()}})
    text = json.dumps({k: d.get(k) for k in ("category", "kicker", "headline", "big_number", "big_label", "what",
                                              "numbers", "details", "points", "watch", "oneliner", "debate", "caption")},
                      ensure_ascii=False)
    prompt = f"""당신은 매거진형 인스타 뉴스 계정 '데일리찝'의 깐깐한 데스크(최종 검수자)입니다. 이미지는 순서대로 1~5장입니다(1=표지, 2=본문 사진 장, 3=세부 내용, 4=정리, 5=에디터 한 줄·토론 질문).
게시물 문구(JSON): {text}

엄격하게 판정하세요. 애매하면 불합격입니다.
1) cover_ok: 표지 사진이 기사 내용을 실제로 보여주거나 자연스럽게 연상시키는가? 기사 상황과 다른 사진(예: 원격수술 기사에 게임기), 기사와 관계없는 상표가 크게 보이는 사진, 엉뚱한 인물 사진은 false. 기사 주제가 그 제품·브랜드라면 상표가 보여도 괜찮음. 사진 없이 어두운 배경이면 true.
2) body_ok: 2장 사진도 같은 기준. 주제와 상관없는 일반 스톡 인물 사진(웃는 모델 등)도 false.
3) text_ok: 제목·표지만 보면 사실과 다르게 오해할 수 있는가(예: 동물 실험인데 사람 수술처럼 보임)? 제목·숫자가 본문과 모순되는가? 확인 안 된 일을 사실처럼 단정했는가? (표현의 과장·자극은 운영자 방침상 허용 — 과장만으로는 불합격 주지 말 것) (수치의 진위는 앞 단계에서 웹 검색으로 확인했으니 출처 표기가 없다는 이유만으로 불합격 주지 말 것) 사망·참사·정치 주제인가? 토론 질문이 혐오·조롱을 유도하는가? 어느 장이든 문장이 중간에 끊기거나(뒷부분이 안 보임) 글자가 겹쳐 보이는가? 문제 있으면 false.
banned: 사망·참사·정치 주제라서 고쳐도 올릴 수 없으면 true.
JSON만 출력: {{"cover_ok":true,"body_ok":true,"text_ok":true,"banned":false,"issues":["문제를 한 줄씩"]}}"""
    msg = Anthropic().messages.create(model=REVIEW_MODEL, max_tokens=6000,
                                      messages=[{"role": "user", "content": imgs + [{"type": "text", "text": prompt}]}])
    log_cost(msg, "review")
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    if "{" not in txt:
        raise ValueError(f"검수 응답에 JSON 없음 (stop={msg.stop_reason}, blocks={[b.type for b in msg.content]}): {txt[:300]}")
    return json.loads(txt[txt.index("{"): txt.rindex("}") + 1])


TEXT_KEYS = ("kicker", "headline", "big_number", "big_label", "what", "numbers", "details", "points", "watch",
             "oneliner", "debate", "caption")


def fix_text(d, issues):
    """검수에서 지적된 문구를 고친다(올리지 않는 대신 수정해서 올림). 자극적인 톤은 유지·강화."""
    from anthropic import Anthropic
    cur = json.dumps({k: d.get(k) for k in TEXT_KEYS}, ensure_ascii=False)
    prompt = f"""인스타 뉴스 카드 '데일리찝'의 문구를 고치세요.
검수 지적: {json.dumps(issues, ensure_ascii=False)}
현재 문구(JSON): {cur}
규칙: 지적된 오해 소지·모순·미확인 단정만 바로잡고, 제목은 오히려 더 자극적으로(숫자·반전·대비). 과장된 표현은 괜찮지만 사실(무슨 일·숫자·대상)은 정확하게.
글자 수는 현재와 비슷하게(카드 레이아웃 유지). 같은 키 구조의 JSON만 출력."""
    msg = Anthropic().messages.create(model=REVIEW_MODEL, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
    log_cost(msg, "fix_text")
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    new = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    for k in TEXT_KEYS:
        if k in new and isinstance(new[k], type(d.get(k))) and new[k]:
            d[k] = new[k]
    print("문구 수정:", d["headline"])


def build_checked(d, out_dir, theme_idx=0):
    """사진 고르기(AI, 기사 상황 기준) → 렌더 → 검수.
    사진 불합격: 다른 후보 1회 → 그래도 안 맞으면 사진 없이(어두운 배경).
    문구 불합격: 지적대로 고쳐서 다시 검수(최대 2회), 그래도 남으면 수정본으로 게시.
    사망·참사·정치(banned)이거나 검수 자체가 실패하면 None(게시 안 함)."""
    used = set()
    try:
        photo, photo2 = choose_photos(d)
    except Exception as e:
        print("사진 선택 실패 → 사진 없이 진행:", e)
        photo, photo2 = None, None
    photo2 = photo2 or photo
    for attempt in range(3):
        for ph in (photo, photo2):
            if ph:
                used.add(ph[2])
        paths = render_slides(d, out_dir, theme_idx=theme_idx, photo=photo, photo2=photo2)
        try:
            rv = review_post(d, out_dir)
        except Exception as e:
            print("검수 실패 → 안전하게 게시 보류:", e)
            health_event("review_error", e)
            return None
        print(f"검수 {attempt + 1}차:", rv)
        d.setdefault("review", []).append(rv)
        if rv.get("banned"):  # 사망·참사·정치 → 고쳐도 올리지 않음
            health_event("review_reject", f"{d['headline']} / {rv.get('issues')}")
            return None
        if not rv.get("text_ok", False):
            if attempt < 2:  # 운영자 방침: 버리지 말고 고쳐서 올림
                try:
                    fix_text(d, rv.get("issues", []))
                except Exception as e:
                    print("문구 수정 실패:", e)
            else:
                health_event("review_text_unfixed", f"{d['headline']} / {rv.get('issues')}")
                return paths  # 두 번 고쳐도 지적이 남으면 마지막 수정본으로 게시(금지 주제는 위에서 이미 걸러짐)
            if attempt >= 1:  # 사진도 계속 불합격이면 사진 없이
                if not rv.get("cover_ok"):
                    photo = None
                if not rv.get("body_ok") or photo is None:
                    photo2 = photo
            if attempt >= 1 or (rv.get("cover_ok") and rv.get("body_ok")):
                continue  # 고친 문구로 다시 렌더·검수 (사진 재선택은 1차에서만)
        if rv.get("cover_ok") and rv.get("body_ok"):
            return paths
        if attempt == 0:  # 검수에서 지적된 사진만 다른 후보로 다시 고름
            try:
                nc, nb = choose_photos(d, exclude=tuple(used), need_cover=not rv.get("cover_ok"),
                                       need_body=not rv.get("body_ok"))
            except Exception as e:
                print("재선택 실패:", e)
                nc = nb = None
            if not rv.get("cover_ok"):
                photo = nc
            if not rv.get("body_ok"):
                photo2 = nb
            photo2 = photo2 or photo
        else:  # 그래도 안 맞으면 사진 없이 (원칙: 엉뚱한 사진보다 어두운 배경)
            if not rv.get("cover_ok"):
                photo = None
            if not rv.get("body_ok") or photo is None:
                photo2 = photo
            health_event("review_photo_drop", d["headline"])
    # 마지막 회차에서 정한 사진(또는 어두운 배경)·문구로 최종 렌더
    return render_slides(d, out_dir, theme_idx=theme_idx, photo=photo, photo2=photo2)


def generate():
    """1단계: 이슈 선정·취재 + 슬라이드 6장 생성 + 게시 전 검수"""
    os.makedirs(OUT_DIR, exist_ok=True)
    if not guard_ok():
        return
    history = load_history()
    d = pick_issue(fetch_news(), history)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M")
    d["stamp"], d["issue_no"] = stamp, len(history) + 1
    record_topic(d)  # 게시 성공 여부와 무관하게 주제를 먼저 기록 → 중복 방지
    paths = build_checked(d, os.path.join(OUT_DIR, stamp), theme_idx=len(history))
    if not paths:
        print("검수 불합격 → 이번 회차는 게시하지 않음:", d["headline"])
        json.dump(d, open(os.path.join(OUT_DIR, stamp, "data.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return
    d["images"] = [f"{stamp}/{os.path.basename(p)}" for p in paths]
    try:
        if os.getenv("POST_REELS", "false") != "true":
            raise RuntimeError("릴스 자동 생성 꺼짐")
        make_reel(paths, os.path.join(OUT_DIR, stamp, "reel.mp4"))
        d["reel"] = f"{stamp}/reel.mp4"
    except Exception as e:
        print("릴스 영상 만들기 실패(캐러셀은 정상 진행):", e)
    json.dump(d, open(os.path.join(OUT_DIR, stamp, "data.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)  # 나중에 다시 그릴 수 있게 보관
    json.dump(d, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(d, ensure_ascii=False, indent=2))


def build_caption(d):
    deb = d.get("debate") or {}
    talk = (f"\n\n💬 {deb['question']}\nA. {deb.get('a', '')}  vs  B. {deb.get('b', '')}\n댓글로 여러분 생각을 남겨주세요 👇"
            if isinstance(deb, dict) and deb.get("question") else "")
    return f"{d['caption']}{talk}\n\n📌 매일 오늘의 이슈를 찝어드려요 → @daily.jjip\n\n{' '.join(d['hashtags'])}"


def publish():
    """2단계: 이미지가 공개 URL에 올라간 뒤 인스타 캐러셀 게시"""
    d = json.load(open(PENDING, encoding="utf-8"))
    deb = d.get("debate") or {}
    talk = (f"\n\n💬 {deb['question']}\nA. {deb.get('a', '')}  vs  B. {deb.get('b', '')}\n댓글로 여러분 생각을 남겨주세요 👇"
            if isinstance(deb, dict) and deb.get("question") else "")
    caption = f"{d['caption']}{talk}\n\n📌 매일 오늘의 이슈를 찝어드려요 → @daily.jjip\n\n{' '.join(d['hashtags'])}"
    if DRY_RUN:
        print("DRY_RUN=true → 게시 생략. posts 폴더의 이미지를 확인하세요.")
        post_id = None
    else:
        try:
            post_id = ig_publish([f"{IMAGE_BASE_URL}/{p}" for p in d["images"]], caption)
        except Exception as e:
            h = health_event("publish_fail", e)
            recent = [x for x in h["events"] if x["kind"] == "publish_fail" and
                      x["at"] >= (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat()]
            if len(recent) >= 2:  # 하루 2번 실제 실패 → 24시간 자동 일시정지 (비용·계정 보호)
                h["paused_until"] = (datetime.datetime.utcnow() + datetime.timedelta(hours=24)).isoformat(timespec="seconds")
                h["pause_reason"] = f"24시간 내 게시 실패 {len(recent)}회: {str(e)[:120]}"
                json.dump(h, open(HEALTH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            raise
        print("게시 완료:", post_id)
        if d.get("reel") and os.getenv("POST_REELS", "false") == "true":  # 운영자 요청으로 릴스 자동 게시 끔 (피드 중복 방지)
            try:
                rid = ig_publish_reel(f"{IMAGE_BASE_URL}/{d['reel']}", caption, f"{IMAGE_BASE_URL}/{d['images'][0]}")
                d["reel_id"] = rid
                print("릴스 게시 완료:", rid)
            except Exception as e:
                print("릴스 게시 실패(캐러셀은 게시됨):", e)
    history = load_history()
    history.append({"date": d["stamp"], "headline": d["headline"],
                    "source": d.get("source_link"), "images": d["images"], "post_id": post_id,
                    "reel_id": d.get("reel_id")})
    json.dump(history, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.remove(PENDING)


def stats():
    """인스타 계정·게시물 성과를 모아 stats/ 폴더에 날짜별로 저장 (주간 보고용)"""
    base = "https://graph.instagram.com/v21.0"
    tok = {"access_token": IG_TOKEN}
    acct = requests.get(f"{base}/me", params={**tok, "fields": "username,followers_count,follows_count,media_count"}, timeout=30).json()
    media, url = [], f"{base}/me/media"
    params = {**tok, "fields": "id,caption,timestamp,like_count,comments_count,permalink,media_type", "limit": 50}
    while url and len(media) < 300:
        r = requests.get(url, params=params, timeout=30).json()
        if "error" in r:
            raise RuntimeError(f"게시물 조회 실패: {r}")
        media += r.get("data", [])
        url, params = (r.get("paging") or {}).get("next"), None
    # 저장·도달 수치는 insights 권한이 있을 때만 수집 (없으면 건너뜀)
    for m in media:
        try:
            ins = requests.get(f"{base}/{m['id']}/insights", params={**tok, "metric": "reach,saved,shares,views"}, timeout=30).json()
            for it in ins.get("data", []):
                m[it["name"]] = (it.get("values") or [{}])[0].get("value", it.get("total_value", {}).get("value"))
        except Exception:
            pass
        m["caption"] = (m.get("caption") or "").split("\n")[0][:80]
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    snap = {"collected_at": now.isoformat(), "account": acct, "media": media}
    os.makedirs("stats", exist_ok=True)
    json.dump(snap, open(f"stats/{now.strftime('%Y%m%d')}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(acct, ensure_ascii=False), "게시물", len(media))


def repost():
    """manual_queue.json 에 있는 이미 만든 세트를 10분 간격으로 하나씩 게시"""
    q = json.load(open("manual_queue.json", encoding="utf-8"))
    history = load_history()
    for i, item in enumerate(q):
        if item.get("post_id"):
            continue
        d = os.path.join(OUT_DIR, item["dir"])
        files = sorted(f for f in os.listdir(d) if f.endswith(".jpg"))
        urls = [f"{IMAGE_BASE_URL}/{item['dir']}/{f}" for f in files]
        try:
            item["post_id"] = ig_publish(urls, item["caption"])
            print("게시 완료:", item["headline"], item["post_id"])
        except Exception as e:
            item["error"] = str(e)[:300]
            print("게시 실패:", item["headline"], e)
        for h in history:
            if h.get("date") == item["dir"]:
                h["post_id"] = item.get("post_id")
        json.dump(q, open("manual_queue.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump(history, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        if any(not x.get("post_id") and not x.get("error") for x in q[i + 1:]):
            time.sleep(600)


def reel_for(dirname):
    """이미 올린 게시물 폴더로 릴스만 만들어 게시 (테스트·보충용)"""
    d = os.path.join(OUT_DIR, dirname)
    slides = sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jpg"))
    make_reel(slides, os.path.join(d, "reel.mp4"))
    return d


def reel_only():
    """캐러셀로 올리지 않은 별도 주제로 전용 릴스 만들기 (하루 1건, 피드 그리드엔 안 뜸)"""
    global FOCUS_OVERRIDE
    if not guard_ok("reel"):
        return
    # 운영자 지시 10/21: 댓글이 쏟아지는 논란·이슈 위주로 최대한 자극적으로(예: 병역특례 0분 출전, 1위 터치 후 실격)
    FOCUS_OVERRIDE = ("릴스 전용(댓글 폭발형 논란): 지금 사람들이 댓글로 싸우고 있는 이슈. 공정성·특혜·규정 논란, '이게 말이 돼?' 싶은 판정·결정, "
                      "돈·연애·결혼·직장·세대 갈등처럼 누구나 한마디 하고 싶은 것 중 분노·억울함·놀람을 부르는 것을 최우선. "
                      "스포츠·연애관계·연예·사회 화제 중에서. 제목·표지는 최대한 자극적으로(숫자·반전·대비). "
                      "단 사실은 검색으로 확인한 것만, 토론 질문은 양쪽 다 말이 되게(특정 개인·집단 비난 유도 금지). "
                      "오늘 캐러셀에서 다룬 주제와 겹치면 안 됨")
    rh_path = "reels_history.json"
    rh = json.load(open(rh_path, encoding="utf-8")) if os.path.exists(rh_path) else []
    used = load_history() + rh  # 캐러셀·릴스에서 이미 다룬 주제 모두 제외
    d = pick_issue(fetch_news(), used)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M")
    d["stamp"], d["issue_no"] = stamp, len(load_history()) + 1
    out = os.path.join("reels", stamp)
    paths = build_checked(d, out)
    if not paths:
        record_topic(d, "reel")  # 불합격 주제도 기록 → 다시 고르지 않음
        print("릴스 검수 불합격 → 오늘 릴스 없음:", d["headline"])
        return
    make_reel(paths, os.path.join(out, "reel.mp4"))
    open(os.path.join(out, "caption.txt"), "w", encoding="utf-8").write(build_caption(d))
    json.dump(d, open(os.path.join(out, "data.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rh.append({"date": stamp, "headline": d["headline"], "topic_key": d.get("topic_key", "")})
    record_topic(d, "reel")
    json.dump(rh, open(rh_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if os.getenv("REEL_AUTOPUBLISH", "false") == "true":
        json.dump({"stamp": stamp}, open("reel_pending.json", "w", encoding="utf-8"))
    # 운영자가 폰에서 받아 음악 붙여 직접 올리도록: 고정 주소(reels/latest.mp4) + 안내 페이지(reels/README.md)
    import shutil
    shutil.copy(os.path.join(out, "reel.mp4"), os.path.join("reels", "latest.mp4"))
    shutil.copy(os.path.join(out, "01.jpg"), os.path.join("reels", "latest_cover.jpg"))
    repo = os.getenv("GITHUB_REPOSITORY", "dailyjjip/daily-jjip")
    kst = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%m/%d %H:%M")
    open(os.path.join("reels", "README.md"), "w", encoding="utf-8").write(
        f"# 오늘의 전용 릴스 ({kst} 생성)\n\n"
        f"**{d['headline']}**\n\n"
        f"1. [영상 받기](https://raw.githubusercontent.com/{repo}/main/reels/latest.mp4) → 길게 눌러 저장\n"
        f"2. 인스타 앱 → 릴스 → 영상 선택 → 음악(추천곡) 지정\n"
        f"3. 아래 캡션 복사해서 붙여넣기 → 공유\n\n"
        f"![표지](latest_cover.jpg)\n\n## 캡션\n\n```\n{build_caption(d)}\n```\n")
    print("릴스 전용 완료:", out, d["headline"])


def comment_lines():
    """홍보용 '댓글용 한 줄' 3개 (운영자가 남의 인기 게시물에 직접 붙여넣기용, 운영자 지시 10/20).
    최근 24시간 데일리찝이 다룬 주제의 data.json에서 정확한 숫자·맥락 한 줄. reels/README.md 하단에 갱신."""
    from anthropic import Anthropic
    cut = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).strftime("%Y%m%d_%H%M")
    items = []
    for base in (OUT_DIR, "reels"):
        for dname in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            path = os.path.join(base, dname, "data.json")
            if dname >= cut and os.path.exists(path):
                d = json.load(open(path, encoding="utf-8"))
                items.append({k: d.get(k) for k in ("headline", "kicker", "what", "numbers", "details", "points")})
    if not items:
        return
    prompt = f"""데일리찝(@daily.jjip) 운영자가 다른 대형 뉴스·정보 계정의 같은 주제 인기 게시물에 직접 달 댓글을 3개 써주세요.
최근 다룬 주제(사실 확인 완료): {json.dumps(items[-8:], ensure_ascii=False)}

조건:
- 화제성 큰 주제 3개를 골라 주제별 1개. 그 게시물 본문엔 없을 법한 정확한 숫자·맥락 한 가지를 담아 "오 이건 몰랐네" 소리가 나게.
- 웃기면서도 진중하게(가벼운 위트 한 스푼 + 정확한 정보). 1~2문장, 70자 안팎. 실제 사람이 쓴 댓글처럼 자연스럽게.
- 금지: 홍보("팔로우", "우리 계정", "@daily.jjip"), 링크, 해시태그, 이모지 2개 이상, 비하·조롱, 정치, 사실이 아닌 내용.
JSON만: [{{"topic": "주제 한 줄", "comment": "댓글"}}]"""
    msg = Anthropic().messages.create(model=REVIEW_MODEL, max_tokens=6000, messages=[{"role": "user", "content": prompt}])
    log_cost(msg, "comment_lines")
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    lines = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    readme = os.path.join("reels", "README.md")
    body = open(readme, encoding="utf-8").read() if os.path.exists(readme) else ""
    body = body.split("\n## 댓글용 한 줄")[0].rstrip()
    kst = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%m/%d")
    sec = [f"\n\n## 댓글용 한 줄 ({kst})", "같은 주제의 인기 게시물(대형 뉴스·정보 계정)이 올라온 직후 30분 안에 붙여넣기. 하루 3~5개, 서로 다른 계정에.\n"]
    for x in lines[:3]:
        sec += [f"**{x.get('topic', '')}**", "```", x.get("comment", ""), "```", ""]
    open(readme, "w", encoding="utf-8").write(body + "\n".join(sec))
    print("댓글용 한 줄:", [x.get("comment") for x in lines[:3]])


def reel_publish():
    """전용 릴스 게시 (영상이 공개 URL에 올라간 뒤). 실패는 캐러셀과 같은 실패 카운트로 관리"""
    stamp = json.load(open("reel_pending.json", encoding="utf-8"))["stamp"]
    base_url = IMAGE_BASE_URL.rsplit("/posts", 1)[0] + "/reels"
    d = json.load(open(f"reels/{stamp}/data.json", encoding="utf-8"))
    if DRY_RUN:
        print("DRY_RUN=true → 릴스 게시 생략")
    else:
        audio = None
        try:
            if FB_TOKEN:  # 추천 음악 우선, 실패하면 음악 없이 기존 방식으로 게시
                try:
                    rid, audio = ig_publish_reel_music(f"{base_url}/{stamp}/reel.mp4", build_caption(d), f"{base_url}/{stamp}/01.jpg")
                except Exception as me:
                    health_event("music_fail", me)
                    print("추천 음악 실패 → 음악 없이 게시:", me)
            if not audio:
                rid = ig_publish_reel(f"{base_url}/{stamp}/reel.mp4", build_caption(d), f"{base_url}/{stamp}/01.jpg")
        except Exception as e:
            h = health_event("publish_fail", f"reel {e}")
            recent = [x for x in h["events"] if x["kind"] == "publish_fail" and
                      x["at"] >= (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat()]
            if len(recent) >= 2:
                h["paused_until"] = (datetime.datetime.utcnow() + datetime.timedelta(hours=24)).isoformat(timespec="seconds")
                h["pause_reason"] = f"24시간 내 게시 실패 {len(recent)}회: {str(e)[:120]}"
                json.dump(h, open(HEALTH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.remove("reel_pending.json")
            raise
        print("전용 릴스 게시 완료:", rid)
        rh = json.load(open("reels_history.json", encoding="utf-8"))
        for x in rh:
            if x.get("date") == stamp:
                x["reel_id"] = rid
                if audio:
                    x["audio_id"], x["audio_title"] = audio["id"], f"{audio.get('artist_name', '')} - {audio.get('title', '')}"
        json.dump(rh, open("reels_history.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.remove("reel_pending.json")


if __name__ == "__main__":
    import sys
    import traceback
    try:
        {"generate": generate, "publish": publish, "stats": stats, "repost": repost, "reel_only": reel_only, "reel_publish": reel_publish, "comment_lines": comment_lines,
         "rebuild_dir": lambda: print("재제작 결과:", (lambda d: (build_checked(d, f"tests/{sys.argv[2]}") and d["headline"], d.get("review")))(
             {k: v for k, v in json.load(open(f"{OUT_DIR}/{sys.argv[2]}/data.json", encoding="utf-8")).items() if k != "review"})),
         "pick_dir": lambda: print("사진 선택 결과:", [(x[1], x[2]) if x else None for x in choose_photos(json.load(open(f"{OUT_DIR}/{sys.argv[2]}/data.json", encoding="utf-8")))]),
         "review_dir": lambda: print("검수 결과:", review_post(json.load(open(f"{OUT_DIR}/{sys.argv[2]}/data.json", encoding="utf-8")), f"{OUT_DIR}/{sys.argv[2]}")),
         "reel_make": lambda: reel_for(sys.argv[2]),
         "reel_post": lambda: print("릴스 게시 완료:", ig_publish_reel(f"{IMAGE_BASE_URL}/{sys.argv[2]}/reel.mp4", build_caption(json.load(open(f"{OUT_DIR}/{sys.argv[2]}/data.json", encoding="utf-8"))), f"{IMAGE_BASE_URL}/{sys.argv[2]}/01.jpg"))}[sys.argv[1]]()
    except Exception:
        open("last_error.txt", "w", encoding="utf-8").write(traceback.format_exc())
        raise
