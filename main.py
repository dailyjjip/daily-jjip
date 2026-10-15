"""
매일 이슈 카드뉴스 자동 생성 + 인스타그램 게시
흐름: 구글뉴스 RSS 수집 → Claude로 이슈 선정·문구 작성 → 비주얼(사진 또는 3D 아이콘) 합성 → 카드 이미지 → 인스타 게시
"""
import os, io, json, datetime, time, random
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


def fetch_news(n=30):
    feed = feedparser.parse(RSS_URL)
    return [{"title": e.title, "link": e.link} for e in feed.entries[:n]]


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


def pick_issue(news, history):
    """Claude가 이슈 1개를 고르고, 웹 검색으로 사실을 확인해 슬라이드 6장 분량의 문구를 만든다."""
    from anthropic import Anthropic
    done = "\n".join(h["headline"] for h in history[-30:])
    items = "\n".join(f"- {x['title']}" for x in news)
    prompt = f"""오늘 한국 뉴스 헤드라인 목록입니다:
{items}

최근 이미 게시한 주제(중복 금지):
{done or '(없음)'}

1) 이 중 일반 대중이 가장 관심 가질 이슈 1개를 고르세요.
2) web_search로 그 이슈의 최신 기사 2~3개를 확인해 구체적인 사실과 수치를 모으세요.
3) 매거진 스타일(아이즈매거진, 디에디트 같은 에디토리얼 톤) 인스타 캐러셀 문구를 쓰세요.
   순서: 표지 → 무슨 일이야 → 세부 내용 → 요약 및 앞으로 주목할 포인트 → 에디터 한 줄 + 토론 질문 → 팔로우

규칙:
- 검색으로 확인한 사실만 사용. 확인 안 된 수치는 쓰지 말 것. 추측·과장·특정인 비방 금지.
- 정치 이슈는 중립적으로. 사망·참사 이슈는 고르지 말 것.
- 유치한 감탄·밈 표현 금지. 짧고 단단한 문장, 잡지 헤드라인처럼 위트 있게.
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
 "image_queries": ["표지용 사진 검색 영어 키워드 2~4단어 (이슈를 한눈에 보여주는 실사 장면, 예: seoul apartment buildings)", "본문용 사진 검색 영어 키워드 2~4단어 (표지와 다른 장면)"],
 "caption": "인스타 본문 3~4문장, 마지막에 저장/공유 유도 한 문장",
 "hashtags": ["#태그", "... 8개"],
 "sources": ["참고한 언론사명 2~3개"],
 "source_title": "근거로 쓴 헤드라인 원문"}}"""
    client = Anthropic()
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    messages = [{"role": "user", "content": prompt}]
    data = None
    for _ in range(4):
        msg = client.messages.create(model=MODEL, max_tokens=8000, tools=tools, messages=messages)
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


def pixabay_photo(query, orientation="portrait", skip_ids=()):
    """Pixabay 무료 사진. 반환: (이미지, 출처 표기, 사진 id) 또는 None"""
    if not PIXABAY_KEY or not query:
        return None
    try:
        r = requests.get("https://pixabay.com/api/", params={
            "key": PIXABAY_KEY, "q": query, "image_type": "photo", "safesearch": "true",
            "orientation": "vertical" if orientation == "portrait" else "horizontal",
            "per_page": 10, "order": "popular"}, timeout=20).json()
        hits = [h for h in (r.get("hits") or []) if h["id"] not in skip_ids]
        if not hits:
            return None
        h = random.choice(hits[:5])
        img = Image.open(io.BytesIO(requests.get(h["largeImageURL"], timeout=30).content)).convert("RGB")
        return img, f"{h.get('user', '')} / Pixabay", h["id"]
    except Exception as e:
        print("Pixabay 사진 실패:", e)
        return None


def get_photo(query, orientation="portrait", skip_ids=()):
    """Pixabay → Pexels 순서로 시도"""
    return pixabay_photo(query, orientation, skip_ids) or pexels_photo(query, orientation, skip_ids)


def pexels_photo(query, orientation="portrait", skip_ids=()):
    """Pexels 무료 사진(상업적 이용 가능, 출처 표기 권장). 실패하면 None.
    반환: (PIL 이미지, 사진작가 이름, 사진 id)"""
    if not PEXELS_KEY or not query:
        return None
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         params={"query": query, "orientation": orientation, "per_page": 10},
                         headers={"Authorization": PEXELS_KEY}, timeout=20).json()
        photos = [p for p in (r.get("photos") or []) if p["id"] not in skip_ids]
        if not photos:
            return None
        p = random.choice(photos[:5])
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
ED_THEMES = [
    ("#F3F0EA", "#141414", "#8A857C", "#D93A1E"),   # 페이퍼 + 레드
    ("#121212", "#F3F0EA", "#7D7A74", "#E8FF3A"),   # 블랙 + 라임
    ("#1F2A44", "#F3F0EA", "#8C95A8", "#FF8A5B"),   # 네이비 + 코랄
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
    img = cover_crop(photo, W, H).convert("RGBA")
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
    f_head = font(SERIF_BLACK, 112)
    lines = wrap(d["headline"], f_head, dr, W - 2 * M)[:2]
    y = H - 150 - len(lines) * 136
    num = d.get("big_number", "")
    if num:
        f_num = fit_font(SERIF_BLACK, num, W - 2 * M, 150, 80, dr)
        bb = dr.textbbox((0, 0), num, font=f_num)
        ny = y - (bb[3] - bb[1]) - 70
        dr.text((M - bb[0], ny - bb[1]), num, font=f_num, fill=accent)
        dr.text((M, ny + (bb[3] - bb[1]) + 14), d.get("big_label", ""), font=font(SANS_B, 32), fill=white)
    for ln in lines:
        dr.text((M, y), ln, font=f_head, fill=white)
        y += 136
    dr.line((M, H - 96, W - M, H - 96), fill=white, width=2)
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
    kick, lines = "3 TIMES A DAY", ["세상 돌아가는 일,", "딱 찝어서."]
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
    dr.text((M, y), "아침 8시 · 점심 12시 · 저녁 7시", font=font(SANS_R, 36), fill=sub)
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
    if photo is not None:
        render_photo_cover(d, p, theme_idx, photo[0], photo[1])
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
def ig_publish(image_urls, caption):
    """이미지 여러 장을 캐러셀(넘겨보기) 게시물로 올린다."""
    base = f"https://graph.instagram.com/v21.0/{IG_USER_ID}"
    children = []
    for u in image_urls:
        r = requests.post(f"{base}/media", data={
            "image_url": u, "is_carousel_item": "true", "access_token": IG_TOKEN}).json()
        if "id" not in r:
            raise RuntimeError(f"슬라이드 업로드 실패: {u} {r}")
        children.append(r["id"])
    r = requests.post(f"{base}/media", data={
        "media_type": "CAROUSEL", "children": ",".join(children),
        "caption": caption, "access_token": IG_TOKEN}).json()
    if "id" not in r:
        raise RuntimeError(f"캐러셀 생성 실패: {r}")
    time.sleep(20)  # 인스타 쪽 처리 대기
    r2 = requests.post(f"{base}/media_publish", data={
        "creation_id": r["id"], "access_token": IG_TOKEN}).json()
    if "id" not in r2:
        raise RuntimeError(f"게시 실패: {r2}")
    return r2["id"]


def generate():
    """1단계: 이슈 선정·취재 + 슬라이드 6장 생성"""
    os.makedirs(OUT_DIR, exist_ok=True)
    history = load_history()
    d = pick_issue(fetch_news(), history)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M")
    d["stamp"], d["issue_no"] = stamp, len(history) + 1
    qs = d.get("image_queries") or [d.get("image_query", "")]
    photo = get_photo(qs[0], "portrait")
    photo2 = get_photo(qs[1] if len(qs) > 1 else qs[0], "landscape",
                          skip_ids=(photo[2],) if photo else ())
    paths = render_slides(d, os.path.join(OUT_DIR, stamp), theme_idx=len(history),
                          photo=photo, photo2=photo2)
    d["images"] = [f"{stamp}/{os.path.basename(p)}" for p in paths]
    json.dump(d, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(d, ensure_ascii=False, indent=2))


def publish():
    """2단계: 이미지가 공개 URL에 올라간 뒤 인스타 캐러셀 게시"""
    d = json.load(open(PENDING, encoding="utf-8"))
    deb = d.get("debate") or {}
    talk = (f"\n\n💬 {deb['question']}\nA. {deb.get('a', '')}  vs  B. {deb.get('b', '')}\n댓글로 여러분 생각을 남겨주세요 👇"
            if isinstance(deb, dict) and deb.get("question") else "")
    caption = f"{d['caption']}{talk}\n\n📌 매일 아침·점심·저녁 이슈를 찝어드려요 → @daily.jjip\n\n{' '.join(d['hashtags'])}"
    if DRY_RUN:
        print("DRY_RUN=true → 게시 생략. posts 폴더의 이미지를 확인하세요.")
        post_id = None
    else:
        post_id = ig_publish([f"{IMAGE_BASE_URL}/{p}" for p in d["images"]], caption)
        print("게시 완료:", post_id)
    history = load_history()
    history.append({"date": d["stamp"], "headline": d["headline"],
                    "source": d.get("source_link"), "images": d["images"], "post_id": post_id})
    json.dump(history, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.remove(PENDING)


if __name__ == "__main__":
    import sys
    import traceback
    try:
        {"generate": generate, "publish": publish}[sys.argv[1]]()
    except Exception:
        open("last_error.txt", "w", encoding="utf-8").write(traceback.format_exc())
        raise
