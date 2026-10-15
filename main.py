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
PEXELS_KEY = os.getenv("PEXELS_API_KEY")                  # 있으면 무료 상업용 사진 사용, 없으면 3D 아이콘
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


def pick_issue(news, history):
    """Claude가 아직 안 다룬 이슈 1개를 골라 카드 문구와 비주얼 키워드를 만든다."""
    from anthropic import Anthropic
    done = "\n".join(h["headline"] for h in history[-30:])
    items = "\n".join(f"- {x['title']}" for x in news)
    prompt = f"""오늘 한국 뉴스 헤드라인 목록입니다:
{items}

최근 이미 게시한 주제(중복 금지):
{done or '(없음)'}

이 중 일반 대중이 가장 관심 가질 이슈 1개를 골라 매거진 스타일(아이즈매거진, 디에디트 같은 에디토리얼 톤) 카드 문구를 쓰세요.
규칙:
- 헤드라인에 있는 사실만 사용. 추측·과장·특정인 비방 금지. 사건사고 피해자 조롱 금지.
- 정치 이슈는 중립적으로. 사망·참사 이슈는 고르지 말 것.
- 유치한 감탄·밈 표현 금지. 짧고 단단한 문장, 잡지 헤드라인처럼 위트 있게.
- 제목과 한줄 코멘트는 서로 다른 내용이어야 함(반복 금지).

아래 JSON만 출력:
{{"category": "경제|사회|IT|생활|국제|스포츠|연예 중 하나",
 "kicker": "제목 위 작은 머리말, 14자 이내 (예: 숫자로 보는 오늘)",
 "headline": "큰 제목, 16자 이내, 두 줄로 나뉘기 좋게",
 "big_number": "이슈를 대표하는 숫자나 짧은 키워드 1개, 6자 이내 (예: 88, 1,400원, 0.1%p). 숫자가 없으면 핵심 단어",
 "big_label": "big_number 설명, 14자 이내 (예: 주 연속 상승)",
 "points": ["핵심 1, 22자 이내", "핵심 2, 22자 이내", "핵심 3, 22자 이내"],
 "oneliner": "에디터 코멘트 한 문장, 26자 이내, 제목과 다른 관점",
 "image_query": "사진 검색용 영어 키워드 2~3단어 (사람 얼굴 없는 사물·풍경)",
 "caption": "인스타 본문 3~4문장, 마지막에 저장/공유 유도 한 문장",
 "hashtags": ["#태그", "... 8개"],
 "source_title": "근거로 쓴 헤드라인 원문"}}"""
    client = Anthropic()
    msg = client.messages.create(model=MODEL, max_tokens=1500,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
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


def pexels_photo(query):
    """Pexels 무료 사진(상업적 이용 가능). 키가 없거나 실패하면 None."""
    if not PEXELS_KEY:
        return None
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         params={"query": query, "orientation": "landscape", "per_page": 5},
                         headers={"Authorization": PEXELS_KEY}, timeout=20).json()
        photos = r.get("photos") or []
        if not photos:
            return None
        url = random.choice(photos)["src"]["large2x"]
        return Image.open(io.BytesIO(requests.get(url, timeout=30).content)).convert("RGB")
    except Exception as e:
        print("사진 가져오기 실패, 3D 아이콘으로 대체:", e)
        return None


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


def render_card(d, path, theme_idx=0, photo=None):
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

    # 헤드라인 (세리프)
    f_head = font(SERIF_BLACK, head_size)
    for ln in wrap(d["headline"], f_head, dr, W - 2 * M)[:2]:
        dr.text((M, y), ln, font=f_head, fill=fg)
        y += int(head_size * 1.22)
    y += 18

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


# ---------- 인스타 게시 ----------
def ig_publish(image_url, caption):
    base = f"https://graph.instagram.com/v21.0/{IG_USER_ID}"
    r = requests.post(f"{base}/media", data={
        "image_url": image_url, "caption": caption, "access_token": IG_TOKEN}).json()
    if "id" not in r:
        raise RuntimeError(f"컨테이너 생성 실패: {r}")
    time.sleep(10)  # 인스타 쪽 이미지 처리 대기
    r2 = requests.post(f"{base}/media_publish", data={
        "creation_id": r["id"], "access_token": IG_TOKEN}).json()
    if "id" not in r2:
        raise RuntimeError(f"게시 실패: {r2}")
    return r2["id"]


def generate():
    """1단계: 이슈 선정 + 카드 이미지 생성"""
    os.makedirs(OUT_DIR, exist_ok=True)
    history = load_history()
    d = pick_issue(fetch_news(), history)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M")
    d["image"], d["stamp"] = f"{stamp}.jpg", stamp
    d["issue_no"] = len(history) + 1
    photo = pexels_photo(d.get("image_query", ""))
    render_card(d, os.path.join(OUT_DIR, d["image"]), theme_idx=len(history), photo=photo)
    json.dump(d, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(d, ensure_ascii=False, indent=2))


def publish():
    """2단계: 이미지가 공개 URL에 올라간 뒤 인스타 게시"""
    d = json.load(open(PENDING, encoding="utf-8"))
    caption = f"{d['caption']}\n\n{' '.join(d['hashtags'])}"
    if DRY_RUN:
        print("DRY_RUN=true → 게시 생략. posts 폴더의 이미지를 확인하세요.")
        post_id = None
    else:
        post_id = ig_publish(f"{IMAGE_BASE_URL}/{d['image']}", caption)
        print("게시 완료:", post_id)
    history = load_history()
    history.append({"date": d["stamp"], "headline": d["headline"],
                    "source": d.get("source_link"), "image": d["image"], "post_id": post_id})
    json.dump(history, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.remove(PENDING)


if __name__ == "__main__":
    import sys
    {"generate": generate, "publish": publish}[sys.argv[1]]()
