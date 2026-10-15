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

이 중 일반 대중이 가장 관심 가질 이슈 1개를 골라 인스타 카드뉴스 문구를 쓰세요.
규칙:
- 헤드라인에 있는 사실만 사용. 추측·과장·특정인 비방 금지. 사건사고 피해자 조롱 금지.
- 정치 이슈는 중립적으로. 사망·참사 이슈는 고르지 말 것.
- 스크롤을 멈추게 할 만큼 짧고 강하게. 해요체 또는 명사형.

아래 JSON만 출력:
{{"category": "경제|사회|IT|생활|국제|스포츠|연예 중 하나",
 "hook": "제목 위 작은 후킹 문구, 12자 이내 (예: 이거 모르면 손해)",
 "headline": "큰 제목, 18자 이내",
 "highlight": "headline 안에서 강조색으로 칠할 핵심 단어(headline에 그대로 포함된 문자열)",
 "points": ["핵심 1, 22자 이내", "핵심 2, 22자 이내", "핵심 3, 22자 이내"],
 "oneliner": "한줄 정리, 24자 이내",
 "emojis": ["주제를 상징하는 이모지 3개 (사물 위주, 사람·국기 제외)"],
 "image_query": "사진 검색용 영어 키워드 2~3단어 (사람 얼굴 없는 사물·풍경)",
 "caption": "인스타 본문 3~4문장, 마지막에 저장/공유 유도 한 문장",
 "hashtags": ["#태그", "... 8개"],
 "source_title": "근거로 쓴 헤드라인 원문"}}"""
    client = Anthropic()
    msg = client.messages.create(model=MODEL, max_tokens=1500,
                                 messages=[{"role": "user", "content": prompt}])
    text = msg.content[0].text
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


# ---------- 카드 렌더링 ----------
def render_card(d, path, theme_idx=0, photo=None):
    top, bottom, accent, fg = THEMES[theme_idx % len(THEMES)]
    img = vgradient(top, bottom).convert("RGBA")
    VIS_H = 640  # 상단 비주얼 영역 높이

    if photo is not None:
        # 사진: 상단을 꽉 채우고 아래로 갈수록 배경색에 녹아들게
        ph = photo.copy()
        scale = max(W / ph.width, (VIS_H + 200) / ph.height)
        ph = ph.resize((int(ph.width * scale), int(ph.height * scale)), Image.LANCZOS)
        ph = ph.crop(((ph.width - W) // 2, 0, (ph.width - W) // 2 + W, VIS_H + 200)).convert("RGBA")
        mask = Image.new("L", ph.size, 255)
        md = ImageDraw.Draw(mask)
        for y in range(ph.height):
            k = max(0, (y - (VIS_H - 240)) / 440)
            md.line((0, y, W, y), fill=int(255 * max(0, 1 - k)))
        img.paste(ph, (0, 0), mask)
        shade = Image.new("RGBA", (W, 260), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        for y in range(260):
            sd.line((0, y, W, y), fill=(0, 0, 0, int(150 * (1 - y / 260))))
        img.alpha_composite(shade)
    else:
        # 3D 아이콘: 빛 번짐 + 큰 메인 아이콘 + 보조 아이콘 2개
        icons = [i for i in (emoji_img(e) for e in d.get("emojis", [])) if i]
        glow(img, (620, 360), 300, accent, 120)
        glow(img, (250, 520), 160, "#FFFFFF", 60)
        # 장식 링
        ring = ImageDraw.Draw(img)
        ring.ellipse((380, 150, 860, 630), outline=hex2rgb(fg) + (70,), width=3)
        if icons:
            paste_icon(img, icons[0], (620, 370), 470)
        if len(icons) > 1:
            paste_icon(img, icons[1], (210, 470), 230, angle=14)
        if len(icons) > 2:
            paste_icon(img, icons[2], (935, 265), 180, angle=-16)

    dr = ImageDraw.Draw(img)
    M = 80
    f_small = font(FONT_REG, 30)
    f_tag = font(FONT_BLACK, 36)

    # 상단: 카테고리 + 날짜 + 화제 스티커
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    tag = d["category"]
    tw = dr.textlength(tag, font=f_tag)
    dr.rounded_rectangle((M, 70, M + tw + 48, 132), radius=31, fill=accent)
    dr.text((M + 24, 76), tag, font=f_tag, fill=bottom)
    dr.text((M + tw + 70, 84), today.strftime("%m.%d") + " 오늘의 이슈", font=f_small, fill=fg)
    f_handle = font(FONT_BLACK, 30)
    dr.text((W - M - dr.textlength(HANDLE, font=f_handle), 84), HANDLE, font=f_handle, fill=fg)

    # 후킹 문구 (기울어진 스티커)
    hook = d.get("hook", "")
    if hook:
        f_hook = font(FONT_BLACK, 40)
        hw = int(dr.textlength(hook, font=f_hook)) + 56
        st = Image.new("RGBA", (hw, 76), (0, 0, 0, 0))
        sd = ImageDraw.Draw(st)
        sd.rounded_rectangle((0, 0, hw - 1, 75), radius=14, fill=fg)
        sd.text((28, 6), hook, font=f_hook, fill=bottom)
        st = st.rotate(-4, resample=Image.BICUBIC, expand=True)
        img.alpha_composite(st, (M - 6, VIS_H - 60))
        dr = ImageDraw.Draw(img)

    # 큰 제목 (강조 단어 컬러)
    f_head = font(FONT_BLACK, 100)
    y = VIS_H + 40
    for ln in wrap(d["headline"], f_head, dr, W - 2 * M)[:2]:
        draw_highlight_line(dr, M, y, ln, d.get("highlight"), f_head, fg, accent)
        y += 122
    y += 26

    # 핵심 포인트 (반투명 박스 안)
    f_pt = font(FONT_REG, 40)
    f_chk = font(FONT_BLACK, 40)
    pts = d["points"][:3]
    box_h = len(pts) * 66 + 40
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((M - 16, y, W - M + 16, y + box_h), radius=24,
                                            fill=(255, 255, 255, 34))
    img.alpha_composite(layer)
    dr = ImageDraw.Draw(img)
    py = y + 22
    for p in pts:
        cx, cy = M + 34, py + 30  # 체크 아이콘 직접 그리기
        dr.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=accent)
        dr.line((cx - 11, cy, cx - 3, cy + 9, cx + 12, cy - 9), fill=bottom, width=6, joint="curve")
        dr.text((M + 74, py), wrap(p, f_pt, dr, W - 2 * M - 90)[0], font=f_pt, fill=fg)
        py += 66

    # 하단 한줄 정리 띠
    f_one = font(FONT_BLACK, 44)
    bar_y = H - 150
    dr.rectangle((0, bar_y, W, bar_y + 96), fill=accent)
    one = wrap(d["oneliner"], f_one, dr, W - 2 * M)[0]
    dr.text(((W - dr.textlength(one, font=f_one)) / 2, bar_y + 18), one, font=f_one, fill=bottom)
    credit = "출처: 언론 보도 종합" + (" · Photo: Pexels" if photo is not None else "")
    dr.text((M, H - 44), credit, font=font(FONT_REG, 24), fill=fg)
    tail = f"매일 콕 찝어주는 {HANDLE}"
    dr.text((W - M - dr.textlength(tail, font=f_small), H - 48), tail, font=f_small, fill=accent)

    img.convert("RGB").save(path, "JPEG", quality=93)


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
