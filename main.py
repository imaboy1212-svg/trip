#!/usr/bin/env python3
"""
trip.bestwellth.org 여행 블로그 자동화 파이프라인
대륙 로테이션 여행지 발굴 → Gemini 콘텐츠 생성 → Unsplash/Pexels 실사 이미지 → WordPress 발행 → Pinterest 연동
"""

import io
import os
import re
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict
from urllib.parse import quote

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup
from PIL import Image
import google.generativeai as genai

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource
try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    _OTLP_AVAILABLE = True
except ImportError:
    _OTLP_AVAILABLE = False

# ==========================================
# 1. 환경 변수
# ==========================================
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY")
UNSPLASH_KEY        = os.getenv("UNSPLASH_ACCESS_KEY", "")
PEXELS_KEY          = os.getenv("PEXELS_API_KEY", "")
WP_SITE_URL         = os.getenv("WP_SITE_URL", "https://trip.bestwellth.org")
WP_USERNAME         = os.getenv("WP_USERNAME")
WP_APP_PASSWORD     = os.getenv("WP_APP_PASSWORD")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
PINTEREST_TOKEN     = os.getenv("PINTEREST_ACCESS_TOKEN")
PINTEREST_BOARD_ID  = os.getenv("PINTEREST_BOARD_ID")
COUPANG_LINK        = os.getenv("COUPANG_PARTNERS_URL", "")
OTLP_ENDPOINT       = os.getenv("OTLP_ENDPOINT", "")
BING_IMAGE_KEY      = os.getenv("BING_IMAGE_SEARCH_KEY", "")
KLOOK_AFFILIATE_ID  = os.getenv("KLOOK_AFFILIATE_ID", "")
GYG_PARTNER_ID      = os.getenv("GYG_PARTNER_ID", "")

# 세시간전(3hoursahead) 제휴 추적 링크 — 고정 커미션 링크
AFF_AGODA    = "https://3ha.in/r/517598"
AFF_EXPEDIA  = "https://3ha.in/r/517604"
AFF_TRIP     = "https://3ha.in/r/517606"
AFF_KLOOK    = "https://3ha.in/r/517607"

for _k, _v in [
    ("GEMINI_API_KEY", GEMINI_API_KEY),
    ("WP_USERNAME",    WP_USERNAME),
    ("WP_APP_PASSWORD", WP_APP_PASSWORD),
]:
    if not _v:
        raise EnvironmentError(f"{_k} 환경변수가 설정되지 않았습니다.")

# ==========================================
# 2. 로깅 및 OpenTelemetry
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("trip_auto")


def setup_telemetry() -> trace.Tracer:
    resource = Resource(attributes={"service.name": "trip-auto-publisher"})
    provider = TracerProvider(resource=resource)
    if _OTLP_AVAILABLE and OTLP_ENDPOINT:
        try:
            exporter = OTLPSpanExporter(endpoint=OTLP_ENDPOINT)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info(f"OTLP exporter 연결: {OTLP_ENDPOINT}")
        except Exception as e:
            logger.warning(f"OTLP 설정 실패, console fallback: {e}")
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("trip_auto")


tracer = setup_telemetry()

# ==========================================
# 3. 색상 상수 (여행 블로그 테마)
# ==========================================
CAT_COLOR        = "#0ea5e9"
CAT_LIGHT_BG     = "#f0f9ff"
CAT_LIGHT_BORDER = "#bae6fd"
CAT_DARK         = "#0369a1"

# ==========================================
# 애드센스 광고 코드
# ==========================================
AD_DISPLAY = """<div style="margin:28px 0;">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-6858780475640766"
     crossorigin="anonymous"></script>
<!-- 디스플레이광고 -->
<ins class="adsbygoogle"
     style="display:block"
     data-ad-client="ca-pub-6858780475640766"
     data-ad-slot="1825484842"
     data-ad-format="auto"
     data-full-width-responsive="true"></ins>
<script>
     (adsbygoogle = window.adsbygoogle || []).push({});
</script>
</div>"""

AD_IN_ARTICLE = """<div style="margin:28px 0;">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-6858780475640766"
     crossorigin="anonymous"></script>
<ins class="adsbygoogle"
     style="display:block"
     data-ad-format="fluid"
     data-ad-layout-key="-5r+d2+3d-69+9m"
     data-ad-client="ca-pub-6858780475640766"
     data-ad-slot="9373370867"></ins>
<script>
     (adsbygoogle = window.adsbygoogle || []).push({});
</script>
</div>"""

AD_AUTORELAXED = """<div style="margin:28px 0;">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-6858780475640766"
     crossorigin="anonymous"></script>
<ins class="adsbygoogle"
     style="display:block"
     data-ad-format="autorelaxed"
     data-ad-client="ca-pub-6858780475640766"
     data-ad-slot="3873632172"></ins>
<script>
     (adsbygoogle = window.adsbygoogle || []).push({});
</script>
</div>"""

# ==========================================
# 4. 픽토그램 (인라인 SVG)
# ==========================================
_PICTOGRAMS: Dict[str, str] = {
    "attraction":    '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/></svg>',
    "food":          '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8h1a4 4 0 010 8h-1"/><path d="M2 8h16v9a4 4 0 01-4 4H6a4 4 0 01-4-4V8z"/><line x1="6" y1="1" x2="6" y2="4"/><line x1="10" y1="1" x2="10" y2="4"/><line x1="14" y1="1" x2="14" y2="4"/></svg>',
    "transport":     '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="3" width="15" height="13" rx="2"/><polygon points="16 8 20 8 23 11 23 16 16 16 16 8"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/></svg>',
    "accommodation": '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>',
    "tips":          '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    "schedule":      '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
    "shopping":      '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2L3 6v14a2 2 0 002 2h14a2 2 0 002-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 01-8 0"/></svg>',
}
_PICTOGRAM_LABELS = {
    "attraction": "주요 명소", "food": "현지 맛집", "transport": "교통 안내",
    "accommodation": "숙소 정보", "tips": "여행 팁", "schedule": "추천 일정", "shopping": "쇼핑 정보",
}


def pictogram_html(key: str) -> str:
    svg   = _PICTOGRAMS.get(key, _PICTOGRAMS["tips"])
    label = _PICTOGRAM_LABELS.get(key, key)
    return (
        f'<span style="display:inline-flex;align-items:center;gap:6px;color:{CAT_COLOR};'
        f'background:{CAT_LIGHT_BG};padding:4px 14px;border-radius:20px;'
        f'font-size:13px;font-weight:700;margin-bottom:14px;">'
        f'{svg}&nbsp;{label}</span>'
    )


# ==========================================
# 5. Gemini 초기화
# ==========================================
genai.configure(api_key=GEMINI_API_KEY)
_supported_models = [
    m.name for m in genai.list_models()
    if "generateContent" in m.supported_generation_methods
]
_preferred = [
    "models/gemini-2.5-flash",
    "models/gemini-2.5-pro",
    "models/gemini-2.0-flash-001",
    "models/gemini-1.5-pro",
    "models/gemini-1.5-flash",
]
GEMINI_MODEL = next(
    (m for m in _preferred if m in _supported_models),
    _supported_models[0] if _supported_models else None,
)
if not GEMINI_MODEL:
    raise RuntimeError("사용 가능한 Gemini 모델이 없습니다.")
gemini = genai.GenerativeModel(GEMINI_MODEL)
logger.info(f"Gemini 모델: {GEMINI_MODEL}")

# ==========================================
# 6. HTTP 헬퍼
# ==========================================
_HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
}


def safe_get(url: str, timeout: int = 15, retries: int = 3, **kwargs) -> Optional[requests.Response]:
    for i in range(retries):
        try:
            r = requests.get(url, headers=_HDRS, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            logger.warning(f"GET 실패 ({i+1}/{retries}) {url}: {e}")
            if i < retries - 1:
                time.sleep(2 ** i)
    return None


# ==========================================
# 7. 대륙 로테이션 기반 여행지 발굴
# ==========================================

# 7일 대륙 로테이션 순서 (6대륙 + 특수 지형)
_ROTATION_ORDER = [
    "Asia",
    "Europe",
    "North America",
    "South America",
    "Africa",
    "Oceania",
    "Special Destinations",  # 해양 섬, 극지방, 미지의 지형
]

# 대륙별 경이로운 장소 후보 풀 (Gemini 보완·fallback용)
_DESTINATION_POOL: Dict[str, List[str]] = {
    "Asia": [
        "Zhangjiajie", "Ha Long Bay", "Cappadocia", "Socotra", "Kawah Ijen",
        "Pamukkale", "Bagan", "Shirakawa-go", "Yakushima", "Jiuzhaigou",
        "Wadi Rum", "Zhangye Danxia", "Palawan", "Raja Ampat", "Komodo",
        "Sapa", "Luang Prabang", "Inle Lake", "Punakha", "Leh Ladakh",
        "Spiti Valley", "Hunza Valley", "Fairy Meadows", "Naran Kaghan",
        "Nusa Penida", "Andaman Islands", "Coorg", "Meghalaya Living Root Bridges",
        "Guilin Li River", "Huanglong", "Jizhaigou Plateau",
        "Hpa-An", "Hsipaw", "Mrauk-U", "Chin State Myanmar",
    ],
    "Europe": [
        "Faroe Islands", "Lofoten", "Trolltunga", "Preikestolen", "Geirangerfjord",
        "Dolomites", "Hallstatt", "Meteora", "Svalbard",
        "Isle of Skye", "Glencoe", "Orkney Islands", "Outer Hebrides",
        "Matera", "Alberobello", "Civita di Bagnoregio", "Setenil de las Bodegas",
        "Bonifacio Corsica", "Calanques de Cassis", "Gorges du Verdon",
        "Etretat Cliffs", "Rocamadour", "Plitvice Lakes",
        "Blagaj Bosnia", "Kravice Falls", "Tara Canyon Montenegro",
        "Rila Mountains", "Rhodope Mountains", "Belogradchik Rocks",
        "Skocjan Caves", "Soča Valley", "Triglav National Park",
    ],
    "North America": [
        "Antelope Canyon", "The Wave Arizona", "Bryce Canyon",
        "Zion Narrows", "Havasupai Falls", "Horseshoe Bend",
        "Mendenhall Glacier", "Inside Passage Alaska", "Kenai Fjords",
        "Na Pali Coast Kauai", "Waimea Canyon", "Haleakala",
        "Copper Canyon Mexico", "Hierve el Agua", "Sumidero Canyon",
        "Cenotes Yucatan", "Palenque", "Calakmul",
        "Nahanni National Park", "Haida Gwaii", "Torngat Mountains",
        "Gros Morne", "Bay of Fundy", "Canadian Badlands",
        "Chiricahua Arizona", "Slot Canyons Utah", "Goblin Valley",
    ],
    "South America": [
        "Torres del Paine", "Los Glaciares", "Carretera Austral",
        "Easter Island", "Marble Caves Chile",
        "Salar de Uyuni", "Laguna Colorada", "Valle de la Luna Bolivia",
        "Rainbow Mountain Peru", "Huacachina", "Colca Canyon",
        "Kaieteur Falls", "Roraima Tepui", "Angel Falls",
        "Lençóis Maranhenses", "Fernando de Noronha", "Chapada Diamantina",
        "Pantanal", "Jalapão", "Ilha Grande",
        "Quebrada de Humahuaca", "Iruya", "Tilcara",
        "Manu National Park", "Yasuni", "Cuyabeno Amazon",
    ],
    "Africa": [
        "Danakil Depression", "Simien Mountains", "Lalibela",
        "Omo Valley", "Bale Mountains Ethiopia",
        "Sossusvlei Namib Desert", "Fish River Canyon", "Skeleton Coast",
        "Okavango Delta", "Makgadikgadi Pans", "Tsodilo Hills",
        "Virunga Mountains", "Bwindi Impenetrable Forest", "Rwenzori Mountains",
        "Tsingy de Bemaraha", "Avenue of the Baobabs", "Andasibe Madagascar",
        "Sahara Merzouga Dunes", "Draa Valley", "Todra Gorge",
        "Bandiagara Escarpment Mali", "Niger Bend",
        "Nyiragongo Volcano", "Lamu Island", "Pemba Island",
        "Kilimanjaro Crater", "Ngorongoro Crater", "Selous Game Reserve",
    ],
    "Oceania": [
        "Milford Sound Fiordland", "Tongariro Alpine Crossing", "Waitomo Caves",
        "Aoraki Mount Cook", "Franz Josef Glacier", "Abel Tasman",
        "Purnululu Bungle Bungle", "Karijini Gorges", "Cape Range",
        "Daintree Rainforest", "Cape Tribulation", "Arnhem Land",
        "Palau Rock Islands", "Nan Madol Micronesia",
        "Vanuatu Tanna Volcano", "Banda Islands Spice Islands",
        "New Caledonia Lagoon", "Lifou Island",
        "Lord Howe Island", "Norfolk Island",
        "Cocos Keeling Islands", "Christmas Island",
        "Kakadu National Park", "Quobba Coast", "The Kimberley",
    ],
    "Special Destinations": [
        "South Georgia Island", "Tristan da Cunha", "St. Helena Island",
        "Svalbard Longyearbyen", "Franz Josef Land", "Jan Mayen",
        "Kerguelen Islands", "Heard Island",
        "Pitcairn Island", "Midway Atoll", "Johnston Atoll",
        "Socotra Island Yemen", "Amsterdam Island Indian Ocean",
        "Azores Islands", "Madeira Island", "Cape Verde",
        "Galápagos Islands", "Cocos Island Costa Rica",
        "Falkland Islands", "South Shetland Islands",
        "Antarctic Peninsula", "Ross Ice Shelf",
        "Maldives Atolls Remote", "Chagos Archipelago",
        "Ogasawara Bonin Islands", "Iriomote Island Japan",
    ],
}

_FALLBACK = [
    "Zhangjiajie", "Faroe Islands", "Havasupai Falls",
    "Torres del Paine", "Danakil Depression", "Milford Sound", "Socotra Island",
]


def get_today_continent() -> str:
    """오늘의 대륙을 7일 로테이션으로 결정합니다."""
    day_of_year = datetime.now().timetuple().tm_yday
    return _ROTATION_ORDER[day_of_year % len(_ROTATION_ORDER)]


def fetch_trending_destinations(published: Optional[set] = None) -> List[str]:
    """대륙 로테이션 + Gemini로 오늘의 경이로운 여행지를 발굴합니다.
    published: 이미 발행된 여행지 셋 (중복 방지)
    """
    with tracer.start_as_current_span("fetch_trending_destinations") as span:
        continent = get_today_continent()
        logger.info(f"오늘의 대륙: {continent}")
        span.set_attribute("continent", continent)

        pool = _DESTINATION_POOL.get(continent, [])
        published_list = ", ".join(list(published)[:30]) if published else "없음"

        prompt = (
            f"You are a travel content strategist targeting Korean travelers.\n\n"
            f"Today's featured continent/region: {continent}\n\n"
            f"Already published destinations (MUST AVOID): {published_list}\n\n"
            f"Candidate pool (can use or ignore): {', '.join(pool[:20])}\n\n"
            f"Select 6 destination PAIRS from {continent}. Each pair = Famous City | Hidden Gem.\n\n"
            f"Rules:\n"
            f"- Famous City: a well-known destination Koreans actively search for (e.g. Chiang Mai, Kyoto, Lisbon, Marrakech)\n"
            f"  NOT the single biggest capital (avoid Paris, Tokyo, Bangkok, Rome, London, New York)\n"
            f"- Hidden Gem: a lesser-known destination reachable from the Famous City within ~3 hours\n"
            f"  that has real travel appeal but thin Korean blog coverage\n"
            f"  (e.g. Chiang Mai | Pai, Kyoto | Amanohashidate, Marrakech | Aït Benhaddou)\n"
            f"- Avoid already-published destinations above\n"
            f"- Both destinations must be in the same country or very nearby region\n\n"
            f"Reply with exactly 6 pairs — one per line in format 'Famous City | Hidden Gem', nothing else."
        )

        try:
            resp = gemini.generate_content(prompt)
            pairs = []
            for line in resp.text.strip().splitlines():
                line = re.sub(r'^[\d\.\-\)\s]+', '', line).strip()
                line = re.sub(r'["""\'*]', '', line).strip()
                if '|' in line:
                    parts = [p.strip() for p in line.split('|', 1)]
                    if len(parts) == 2 and all(2 <= len(p) <= 60 for p in parts):
                        pairs.append(f"{parts[0]} | {parts[1]}")
            if pairs:
                logger.info(f"Gemini 발굴 여행지 쌍 ({continent}): {pairs}")
                span.set_attribute("source", f"gemini+{continent}")
                span.set_attribute("destinations.count", len(pairs))
                return pairs[:6]
        except Exception as e:
            logger.warning(f"Gemini 여행지 발굴 실패: {e}")

        # Fallback: pool에서 단일 항목 → 더미 쌍으로 구성
        import random
        candidates = [d for d in pool if not published or d not in published]
        if not candidates:
            candidates = pool.copy()
        random.shuffle(candidates)
        logger.warning(f"Gemini 실패 — pool fallback ({continent})")
        span.set_attribute("source", "pool_fallback")
        fallback = candidates[:6] if candidates else _FALLBACK.copy()
        return [f"{d} | {d}" for d in fallback]


# ==========================================
# 8. 가이드북 스타일 추출
# ==========================================

def fetch_guidebook_style(destination: str) -> str:
    with tracer.start_as_current_span("fetch_guidebook_style") as span:
        enc = quote(destination.replace(" ", "_"))
        for base in ["https://wikitravel.org/en/", "https://en.wikivoyage.org/wiki/"]:
            resp = safe_get(base + enc, timeout=15)
            if not resp:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            content = soup.select_one("#mw-content-text")
            if content:
                paras = [p.get_text(" ", strip=True) for p in content.select("p") if len(p.get_text(strip=True)) > 60]
                if paras:
                    style = "\n".join(paras[:6])
                    span.set_attribute("style.length", len(style))
                    return style[:2000]
        return (
            "객관적이고 전문적인 여행 가이드북 형식. "
            "명소·맛집·교통을 항목별로 정확하게 기술. "
            "개인 경험이나 일기 형식 완전 배제. 간결하고 실용적인 정보 중심."
        )


# ==========================================
# 9. 여행지 데이터 수집 (fallback 포함)
# ==========================================

def fetch_travel_data(destination: str) -> Dict:
    with tracer.start_as_current_span("fetch_travel_data") as span:
        span.set_attribute("destination", destination)
        data: Dict = {
            "destination": destination,
            "overview": "", "attractions": "", "food": "",
            "transport": "", "accommodation": "", "tips": "",
            "sources": [],
        }
        enc = quote(destination.replace(" ", "_"))
        for base in ["https://wikitravel.org/en/", "https://en.wikivoyage.org/wiki/"]:
            resp = safe_get(base + enc, timeout=15)
            if not resp:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            content = soup.select_one("#mw-content-text")
            if not content or len(content.get_text(strip=True)) < 400:
                continue
            data["overview"] = content.get_text(" ", strip=True)[:3000]
            data["sources"].append(base + enc)
            for h in content.select("h2, h3"):
                tl = h.get_text(strip=True).lower()
                buf = []
                nxt = h.find_next_sibling()
                while nxt and nxt.name not in ("h2", "h3"):
                    buf.append(nxt.get_text(" ", strip=True))
                    nxt = nxt.find_next_sibling()
                text = " ".join(buf).strip()
                if any(k in tl for k in ["see", "do", "attraction", "sight", "visit"]):
                    data["attractions"] += text[:1500]
                elif any(k in tl for k in ["eat", "drink", "food", "restaurant"]):
                    data["food"] += text[:1200]
                elif any(k in tl for k in ["get in", "get around", "transport", "bus", "train"]):
                    data["transport"] += text[:800]
                elif any(k in tl for k in ["sleep", "stay", "accommodation", "hotel"]):
                    data["accommodation"] += text[:800]
                elif any(k in tl for k in ["tip", "know", "respect", "cope"]):
                    data["tips"] += text[:500]
            break

        if len(data["overview"]) < 300:
            q = quote(f"{destination} 여행 관광지 맛집 교통 숙소")
            resp = safe_get(f"https://www.bing.com/search?q={q}", timeout=15)
            if resp:
                soup = BeautifulSoup(resp.text, "html.parser")
                snippets = " ".join(
                    el.get_text(" ", strip=True)
                    for el in soup.select(".b_caption p, .b_algo p")[:12]
                )
                data["overview"] += snippets
                data["sources"].append(f"https://www.bing.com/search?q={q}")

        total = sum(len(v) for v in data.values() if isinstance(v, str))
        span.set_attribute("data.total_chars", total)

        # Gemini 폴백 — wikitravel/Bing 모두 실패 시 Gemini로 기본 정보 생성
        if total < 300:
            logger.warning(f"'{destination}' 웹 데이터 부족 ({total}자) — Gemini 폴백 사용")
            try:
                prompt = (
                    f"You are a travel expert. Provide factual travel information about '{destination}' "
                    f"in Korean for a travel blog. Include:\n"
                    f"- Overview (2-3 sentences)\n"
                    f"- Top 3 attractions with brief descriptions\n"
                    f"- Local food specialties\n"
                    f"- How to get there and get around\n"
                    f"- Accommodation options\n"
                    f"- Practical travel tips\n"
                    f"Be factual and specific. No markdown symbols."
                )
                resp = gemini.generate_content(prompt)
                gemini_text = resp.text.strip()
                if len(gemini_text) > 200:
                    data["overview"] = gemini_text[:3000]
                    data["attractions"] = gemini_text[:1500]
                    data["sources"].append("Gemini AI")
                    total = sum(len(v) for v in data.values() if isinstance(v, str))
                    logger.info(f"'{destination}' Gemini 폴백 완료 ({total}자)")
            except Exception as e:
                logger.warning(f"Gemini 폴백 실패: {e}")

        total = sum(len(v) for v in data.values() if isinstance(v, str))
        if total < 300:
            raise ValueError(f"'{destination}' 데이터 불충분 ({total}자)")
        logger.info(f"'{destination}' 데이터 수집 완료 ({total}자)")
        return data


# ==========================================
# 10. 포토그래픽팀 — 이미지 수집 (중복 방지)
# ==========================================

# ==========================================
# 10-A. 포토그래픽팀 — 섹션별 검색 쿼리
# ==========================================

_PHOTO_QUERIES: Dict[str, List[str]] = {
    "featured":   [
        "{d} aerial panorama landscape",
        "{d} cityscape skyline",
        "{d} scenic view travel photography",
    ],
    "portrait":   [
        "{d} local people culture portrait",
        "{d} traditional culture lifestyle",
        "{d} community life people",
    ],
    "attraction": [
        "{d} famous landmark heritage",
        "{d} historic monument architecture",
        "{d} UNESCO world heritage site",
    ],
    "food":       [
        "{d} traditional food dish",
        "{d} local cuisine restaurant",
        "{d} street food",
        "{d} foods",
    ],
    "transport":  [
        "{d} airport train station transportation",
        "{d} public transit city transport",
        "{d} scenic route road journey",
    ],
    "tips":       [
        "{d} nature wilderness landscape",
        "{d} outdoor adventure travel",
        "{d} scenic hiking trail",
    ],
}

def _is_photo_quality(img_bytes: bytes, min_w: int = 500, min_h: int = 350) -> bool:
    """여행 사진으로 적합한지 검증합니다.
    - 최소 해상도 확인
    - 흰색 배경 제품 사진 거부 (평균 밝기 > 235)
    - 색상 다양성 부족한 단색 이미지 거부 (삽화·다이어그램 방지)
    """
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        if w < min_w or h < min_h:
            return False
        if w / h > 4.0 or h / w > 4.0:
            return False
        # 썸네일로 다운샘플 후 색상 분석
        thumb = img.resize((80, 50))
        pixels = list(thumb.getdata())
        n = len(pixels)
        avg_brightness = sum((r + g + b) / 3 for r, g, b in pixels) / n
        if avg_brightness > 235:  # 흰 배경 제품 사진
            return False
        # 색상 표준편차 — 너무 낮으면 단색(삽화·다이어그램)
        avg_r = sum(p[0] for p in pixels) / n
        avg_g = sum(p[1] for p in pixels) / n
        avg_b = sum(p[2] for p in pixels) / n
        variance = sum((p[0]-avg_r)**2 + (p[1]-avg_g)**2 + (p[2]-avg_b)**2 for p in pixels) / n
        if variance < 200:  # 거의 단색
            return False
        return True
    except Exception:
        return False


def _fetch_url(url: str, used_urls: set, min_bytes: int = 40000) -> Optional[bytes]:
    """이미지 URL 다운로드 + 품질 검증."""
    if url in used_urls:
        return None
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,*/*;q=0.8",
                "Referer": "https://www.google.com/",
            },
            timeout=25,
            allow_redirects=True,
        )
        if r.status_code != 200 or len(r.content) < min_bytes:
            return None
        if "image" not in r.headers.get("Content-Type", ""):
            # Content-Type이 없으면 확장자로 판단
            if not any(url.lower().split("?")[0].endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
                return None
        if not _is_photo_quality(r.content):
            return None
        used_urls.add(url)
        return r.content
    except Exception:
        return None


def _bing_api_search(query: str, orientation: str, used_urls: set) -> Optional[bytes]:
    """Bing Image Search API (BING_IMAGE_SEARCH_KEY 설정 시 사용)."""
    if not BING_IMAGE_KEY:
        return None
    try:
        aspect = "Wide" if orientation == "landscape" else "Tall"
        resp = requests.get(
            "https://api.bing.microsoft.com/v7.0/images/search",
            params={
                "q": query,
                "count": 20,
                "imageType": "Photo",
                "license": "Public",
                "aspect": aspect,
                "safeSearch": "Moderate",
            },
            headers={"Ocp-Apim-Subscription-Key": BING_IMAGE_KEY},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        for item in resp.json().get("value", []):
            img_url = item.get("contentUrl", "")
            if not img_url:
                continue
            data = _fetch_url(img_url, used_urls)
            if data:
                logger.info(f"[Bing API] {query[:40]} → {img_url[:70]}")
                return data
    except Exception as e:
        logger.debug(f"Bing API 실패 ({query[:30]}): {e}")
    return None


def _unsplash_search(query: str, orientation: str, used_urls: set, per_page: int = 30) -> Optional[bytes]:
    """Unsplash API — 여행 전문 큐레이션 사진."""
    if not UNSPLASH_KEY:
        return None
    try:
        import random
        page = random.randint(1, 3)
        resp = requests.get(
            "https://api.unsplash.com/search/photos",
            params={
                "query": query,
                "orientation": orientation,
                "content_filter": "high",
                "per_page": per_page,
                "page": page,
                "order_by": "relevant",
            },
            headers={"Authorization": f"Client-ID {UNSPLASH_KEY}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        random.shuffle(results)
        for photo in results:
            url = photo["urls"]["regular"]  # 1080px — full보다 빠름
            data = _fetch_url(url, used_urls, min_bytes=20000)
            if data:
                logger.info(f"[Unsplash] {query[:40]} → {url[:60]}")
                return data
    except Exception as e:
        logger.debug(f"Unsplash 오류 ({query[:30]}): {e}")
    return None


def _pexels_search(query: str, orientation: str, used_urls: set, per_page: int = 30) -> Optional[bytes]:
    """Pexels API — 고품질 여행 사진."""
    if not PEXELS_KEY:
        return None
    try:
        import random
        page = random.randint(1, 3)
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            params={
                "query": query,
                "orientation": "landscape" if orientation == "landscape" else "portrait",
                "per_page": per_page,
                "page": page,
                "size": "large",
            },
            headers={"Authorization": PEXELS_KEY},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        photos = resp.json().get("photos", [])
        random.shuffle(photos)
        for photo in photos:
            url = photo["src"].get("large2x") or photo["src"]["original"]
            data = _fetch_url(url, used_urls, min_bytes=20000)
            if data:
                logger.info(f"[Pexels] {query[:40]} → {url[:60]}")
                return data
    except Exception as e:
        logger.debug(f"Pexels 오류 ({query[:30]}): {e}")
    return None


def _get_official_tourism_urls(destination: str) -> List[str]:
    """Gemini로 여행지 공식 관광 사이트 URL을 파악합니다."""
    try:
        prompt = (
            f"List the official tourism websites for the travel destination: '{destination}'.\n"
            f"Include: national/regional tourism board, official city tourism portal, or official national park site.\n"
            f"Output ONLY valid, working URLs — one per line (max 3). No explanation, no numbering.\n"
            f"If you are not confident a URL exists and works, do not include it."
        )
        model = genai.GenerativeModel("gemini-2.0-flash")
        resp = model.generate_content(prompt)
        urls = []
        for line in resp.text.strip().splitlines():
            line = line.strip().strip(".-*• ")
            if line.startswith("http") and "." in line and len(line) < 120:
                urls.append(line)
        return urls[:3]
    except Exception as e:
        logger.debug(f"공식 URL 파악 실패 ({destination}): {e}")
        return []


def _crawl_official_site_images(url: str, used_hashes: set, max_images: int = 8) -> List[bytes]:
    """공식 관광 사이트에서 고품질 이미지를 크롤링합니다."""
    images = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        if resp.status_code != 200:
            logger.debug(f"공식 사이트 접근 실패 {url}: {resp.status_code}")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        base_url = "/".join(url.split("/")[:3])

        # srcset, data-src, src 순으로 가장 큰 이미지 URL 추출
        img_urls: List[str] = []
        for img_tag in soup.find_all("img"):
            src = ""
            # srcset에서 가장 큰 해상도 선택
            srcset = img_tag.get("srcset") or img_tag.get("data-srcset") or ""
            if srcset:
                candidates = []
                for part in srcset.split(","):
                    part = part.strip()
                    tokens = part.split()
                    if tokens:
                        s_url = tokens[0]
                        width = int(tokens[1].rstrip("w")) if len(tokens) > 1 and tokens[1].endswith("w") else 0
                        candidates.append((width, s_url))
                if candidates:
                    src = max(candidates, key=lambda x: x[0])[1]
            if not src:
                src = img_tag.get("data-src") or img_tag.get("src") or ""
            if not src:
                continue
            # 상대 URL 처리
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = base_url + src
            elif not src.startswith("http"):
                continue
            # 아이콘·로고·썸네일 제외
            src_lower = src.lower()
            if any(x in src_lower for x in ["icon", "logo", "thumb", "avatar", "button", "sprite", ".svg", ".gif", "1x1"]):
                continue
            img_urls.append(src)

        # 중복 제거 후 다운로드
        seen = set()
        for img_url in img_urls:
            if img_url in seen:
                continue
            seen.add(img_url)
            try:
                r = requests.get(img_url, headers=headers, timeout=15)
                if r.status_code != 200 or len(r.content) < 40000:
                    continue
                img_hash = hash(r.content[:2048])
                if img_hash in used_hashes:
                    continue
                if not _is_photo_quality(r.content):
                    continue
                used_hashes.add(img_hash)
                images.append(r.content)
                logger.info(f"[공식사이트] 이미지 수집: {img_url[:70]}")
                if len(images) >= max_images:
                    break
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"공식 사이트 크롤링 실패 ({url}): {e}")
    return images


def _get_transport_queries(destination: str, transport_services: List[str], transport_text: str) -> List[str]:
    """여행지 실제 교통수단 목록에서 구체적인 이미지 검색 쿼리를 생성합니다.

    우선순위:
    1. transport_services 각 항목 → "{destination} {service}" 직접 검색
    2. 공항 사진 → "{destination} airport"
    3. 섬/해안 지형이면 페리/선박 → "{destination} ferry" or "{destination} boat"
    4. 상위 지역 교통편 → parent region + 첫 번째 서비스
    """
    # 영문 교통수단 정규화 맵 (한글/혼용 대응)
    _TRANSPORT_KW_MAP = {
        "택시": "taxi", "렌터카": "rental car", "렌트카": "rental car",
        "버스": "bus", "페리": "ferry", "배": "ferry boat", "보트": "boat",
        "기차": "train", "철도": "train", "지하철": "subway metro",
        "헬리콥터": "helicopter", "자전거": "bicycle cycling",
        "오토바이": "motorcycle", "트램": "tram",
        "ferry": "ferry", "bus": "bus", "taxi": "taxi",
        "train": "train", "car": "car rental", "rental": "rental car",
        "boat": "boat", "ship": "ship ferry", "helicopter": "helicopter",
        "bicycle": "bicycle", "tram": "tram", "subway": "subway metro",
    }

    # 서비스 목록을 영문 키워드로 변환
    def _to_eng(svc: str) -> str:
        svc_lower = svc.lower().strip()
        for k, v in _TRANSPORT_KW_MAP.items():
            if k in svc_lower:
                return v
        return svc_lower

    eng_services = [_to_eng(s) for s in transport_services[:5]]

    # 우선순위: bus → airport → taxi → 나머지 서비스 → ferry(섬) → 상위 지역
    queries: List[str] = []

    # 1순위: 버스 (사진이 가장 많고 목적지 식별이 명확)
    bus_kws = {"bus", "버스", "tram", "트램"}
    for eng in eng_services:
        if any(k in eng for k in bus_kws):
            queries.append(f"{destination} {eng}")
            break

    # 2순위: 공항 (어떤 여행지든 사진이 풍부하고 명확)
    queries.append(f"{destination} airport")

    # 3순위: 택시
    taxi_kws = {"taxi", "택시"}
    for eng in eng_services:
        if any(k in eng for k in taxi_kws):
            queries.append(f"{destination} {eng}")
            break

    # 4순위: 나머지 교통수단 (렌터카, 기차, 헬리콥터 등)
    skip_kws = bus_kws | taxi_kws
    for eng in eng_services:
        if not any(k in eng for k in skip_kws):
            queries.append(f"{destination} {eng}")

    # 5순위: 섬/해안 지형이면 페리·보트 추가
    island_kw = ["island", "islands", "isle", "ferry", "섬", "페리", "해협"]
    if any(kw in transport_text.lower() for kw in island_kw):
        queries.append(f"{destination} ferry boat")
        queries.append(f"{destination} scenic coastal road")

    # 6순위: 상위 지역명 fallback
    parent = destination.split()[-1] if " " in destination else destination
    if parent != destination:
        queries.append(f"{parent} airport")
        queries.append(f"{parent} transportation")

    return queries


def fetch_travel_image(
    destination: str,
    orientation: str = "landscape",
    query: str = "",
    section: str = "featured",
    used_urls: Optional[set] = None,
) -> Optional[bytes]:
    """포토그래픽팀 — 섹션별 정교한 쿼리로 여행 사진을 수집합니다.
    우선순위: Unsplash API → Pexels API → Bing Image Search API(키 있을 때)
    used_urls를 공유해 동일 글 내 중복 이미지를 차단합니다.
    """
    with tracer.start_as_current_span("fetch_travel_image") as span:
        span.set_attribute("destination", destination)
        span.set_attribute("section", section)
        if used_urls is None:
            used_urls = set()

        if query:
            queries = [query]
        else:
            templates = _PHOTO_QUERIES.get(section, _PHOTO_QUERIES["featured"])
            queries = [t.replace("{d}", destination) for t in templates]

        for q in queries:
            # 1순위: Unsplash (여행 전문 큐레이션, 가장 정확)
            result = _unsplash_search(q, orientation, used_urls)
            if result:
                span.set_attribute("source", "unsplash")
                span.set_attribute("found_query", q)
                return result
            # 2순위: Pexels
            result = _pexels_search(q, orientation, used_urls)
            if result:
                span.set_attribute("source", "pexels")
                span.set_attribute("found_query", q)
                return result
            # 3순위: Bing Image Search API (키 있을 때만)
            result = _bing_api_search(q, orientation, used_urls)
            if result:
                span.set_attribute("source", "bing_api")
                span.set_attribute("found_query", q)
                return result

        logger.warning(f"[포토그래픽팀] 이미지 없음 — 패스 ({destination} / {section})")
        return None


def crop_to_ratio(img_bytes: bytes, width: int, height: int) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    target_ratio = width / height
    src_ratio    = img.width / img.height
    if src_ratio > target_ratio:
        new_w = int(img.height * target_ratio)
        left  = (img.width - new_w) // 2
        img   = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / target_ratio)
        top   = (img.height - new_h) // 2
        img   = img.crop((0, top, img.width, top + new_h))
    img = img.resize((width, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True)
    return buf.getvalue()


# ==========================================
# 11. 예약 버튼 유틸 (포토그래픽팀 이후)
# ==========================================

_BTN_STYLE = (
    "display:inline-block;padding:10px 22px;border-radius:8px;"
    "font-size:14px;font-weight:700;text-decoration:none;color:#fff;"
    "margin:4px 8px 4px 0;"
)


_CRAWL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_TABLE_ROW_STYLES = ["background:#fff;border-bottom:1px solid #e2e8f0;",
                     "background:#f8fafc;border-bottom:1px solid #e2e8f0;"]


def _build_options_table(rows: List[Dict], title: str, headers: List[str], cat_color: str) -> str:
    if not rows:
        return ""
    th = "".join(f'<th style="padding:10px 14px;text-align:center;">{h}</th>' for h in headers)
    tbody = ""
    for i, row in enumerate(rows):
        style = _TABLE_ROW_STYLES[i % 2]
        cells = "".join(
            f'<td style="padding:10px 14px;{"text-align:center;font-weight:600;" if j==0 else ""}">{v}</td>'
            for j, v in enumerate(row.get("cells", []))
        )
        tbody += f'<tr style="{style}">{cells}</tr>'
    return (
        f'<h4 style="font-size:15px;font-weight:700;color:#0f172a;margin:20px 0 10px 0;">{title}</h4>'
        f'<table style="width:100%;border-collapse:collapse;margin:0 0 16px;font-size:14px;">'
        f'<thead><tr style="background:#0c4a6e;color:#fff;">{th}</tr></thead>'
        f'<tbody>{tbody}</tbody></table>'
    )


def crawl_getyourguide(attraction: str, destination: str) -> List[Dict]:
    """GetYourGuide에서 관광지 티켓 옵션 크롤링."""
    try:
        query = f"{attraction} {destination}"
        resp = requests.get(
            "https://www.getyourguide.com/s/",
            params={"q": query, "currency": "USD"},
            headers=_CRAWL_HEADERS, timeout=12,
        )
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        options = []
        for card in soup.select("[data-test='activity-card'], .activity-card, [class*='ActivityCard']")[:5]:
            title_el = card.select_one("[class*='title'], h3, h2")
            price_el = card.select_one("[class*='price'], [data-test='price']")
            if title_el:
                title = title_el.get_text(strip=True)
                price = price_el.get_text(strip=True) if price_el else "현지 가격 확인"
                fast = "패스트트랙" if any(k in title.lower() for k in ["skip", "fast", "priority", "express"]) else ""
                options.append({"cells": [title + (f" [{fast}]" if fast else ""), price, "GetYourGuide"]})
        return options
    except Exception as e:
        logger.debug(f"GYG 크롤링 실패 ({attraction}): {e}")
        return []


def crawl_klook(attraction: str, destination: str) -> List[Dict]:
    """Klook에서 관광지 티켓 옵션 크롤링."""
    try:
        query = f"{attraction} {destination}"
        resp = requests.get(
            "https://www.klook.com/search/",
            params={"query": query, "cat": "attraction"},
            headers=_CRAWL_HEADERS, timeout=12,
        )
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        options = []
        for card in soup.select("[class*='ActivityCard'], [class*='product-card'], .search-result-item")[:5]:
            title_el = card.select_one("[class*='title'], h3")
            price_el = card.select_one("[class*='price']")
            if title_el:
                title = title_el.get_text(strip=True)
                price = price_el.get_text(strip=True) if price_el else "현지 가격 확인"
                options.append({"cells": [title, price, "Klook"]})
        return options
    except Exception as e:
        logger.debug(f"Klook 크롤링 실패 ({attraction}): {e}")
        return []


def crawl_booking_hotel(hotel_name: str, destination: str, hotel_type: str = "hotel") -> List[Dict]:
    """Booking.com에서 숙소 객실 등급 크롤링."""
    try:
        query = f"{hotel_name} {destination}"
        resp = requests.get(
            "https://www.booking.com/search.html",
            params={"ss": query, "lang": "en-us", "currency": "USD"},
            headers=_CRAWL_HEADERS, timeout=12,
        )
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        options = []
        for prop in soup.select("[data-testid='property-card'], .sr_property_block")[:1]:
            for room in prop.select("[data-testid='recommended-units'], .room-type")[:5]:
                name_el = room.select_one("[data-testid='recommended-units-item-title'], .room-title")
                price_el = room.select_one("[data-testid='price-and-discounted-price'], .price")
                if name_el:
                    options.append({
                        "cells": [
                            name_el.get_text(strip=True),
                            price_el.get_text(strip=True) if price_el else "가격 확인 필요",
                            "Booking.com 기준",
                        ]
                    })
        return options
    except Exception as e:
        logger.debug(f"Booking.com 크롤링 실패 ({hotel_name}): {e}")
        return []


def build_attraction_ticket_table(attractions: List[str], destination: str, cat_color: str) -> str:
    """관광지별 티켓 옵션 테이블 HTML 생성."""
    html = ""
    headers = ["티켓 종류", "가격", "플랫폼"]
    for attraction in attractions[:4]:
        options = crawl_getyourguide(attraction, destination) or crawl_klook(attraction, destination)
        if options:
            html += _build_options_table(options, f"{attraction} 티켓 옵션", headers, cat_color)
    return html


def build_accommodation_table(hotels: List[str], destination: str, cat_color: str) -> str:
    """숙소별 객실 등급 테이블 HTML 생성."""
    html = ""
    headers = ["객실 등급", "1박 기준 요금", "특징"]
    for entry in hotels[:3]:
        parts = entry.split("|")
        hotel_name = parts[0].strip()
        hotel_type = parts[1].strip() if len(parts) > 1 else "hotel"
        options = crawl_booking_hotel(hotel_name, destination, hotel_type)
        if options:
            html += _build_options_table(options, f"{hotel_name} 객실 등급", headers, cat_color)
    return html


def build_transport_classes_table(services: List[str], destination: str, cat_color: str) -> str:
    """교통편 클래스 비교 테이블 HTML 생성 (Gemini로 조회)."""
    if not services or not gemini:
        return ""
    service_list = "\n".join(f"- {s}" for s in services[:3])
    prompt = (
        f"For the following transport services relevant to {destination}, "
        f"list the available seat/cabin classes in order from lowest to highest tier.\n"
        f"{service_list}\n\n"
        f"For each service and class, output exactly:\n"
        f"ServiceName|ClassName|PriceRange(USD)|KeyDifferences\n"
        f"Output only the data lines. No explanations. No markdown."
    )
    try:
        resp = gemini.generate_content(prompt)
        lines = [l.strip() for l in resp.text.strip().splitlines() if l.count("|") >= 3]
        if not lines:
            return ""
        from itertools import groupby
        html = ""
        headers = ["클래스", "요금 기준 (USD)", "주요 차이점"]
        current_service = None
        service_rows: List[Dict] = []
        for line in lines:
            parts = [p.strip() for p in line.split("|")]
            svc, cls_name, price, diff = parts[0], parts[1], parts[2], parts[3]
            if svc != current_service:
                if current_service and service_rows:
                    html += _build_options_table(service_rows, f"{current_service} 클래스 비교", headers, cat_color)
                current_service = svc
                service_rows = []
            service_rows.append({"cells": [cls_name, price, diff]})
        if current_service and service_rows:
            html += _build_options_table(service_rows, f"{current_service} 클래스 비교", headers, cat_color)
        return html
    except Exception as e:
        logger.warning(f"교통 클래스 조회 실패: {e}")
        return ""


def validate_url(url: str, timeout: int = 10) -> bool:
    """HEAD → GET fallback으로 URL 유효성 검사. 405/403은 GET으로 재시도."""
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=ua)
        if r.status_code in (405, 403):
            # HEAD를 차단하는 사이트 → GET으로 재시도
            r = requests.get(url, timeout=timeout, allow_redirects=True, headers=ua,
                             stream=True)
            r.close()
        return r.status_code < 400
    except Exception:
        return False


def validate_urls(entries: List[tuple]) -> List[tuple]:
    valid = []
    for name, url in entries:
        if validate_url(url):
            valid.append((name, url))
            logger.info(f"URL 유효: {name} → {url}")
        else:
            logger.info(f"URL 유효성 실패, 버튼 제외: {name} → {url}")
    return valid


_BTN_WRAP = (
    'display:flex;flex-wrap:wrap;gap:10px;justify-content:center;'
    'margin:20px 0 12px 0;'
)

def build_action_buttons(
    entries: List[tuple],
    label_suffix: str,
    bg_color: str,
    fallback_text: str = "",
) -> str:
    """유효한 URL이 있으면 버튼, 없으면 fallback_text(안내 문구)를 반환합니다."""
    if not entries:
        if fallback_text:
            return (
                f'<p style="font-size:13px;color:#64748b;margin:12px 0;text-align:center;">'
                f'{fallback_text}</p>'
            )
        return ""
    btns = "".join(
        f'<a href="{url}" target="_blank" rel="nofollow noopener" '
        f'style="{_BTN_STYLE}background:{bg_color};">{name} {label_suffix}</a>'
        for name, url in entries
    )
    return f'<div style="{_BTN_WRAP}">{btns}</div>'


def build_hotel_buttons(destination: str) -> str:
    dest_enc = quote(destination)
    hotels = [
        ("Agoda", f"https://www.agoda.com/search?city={dest_enc}", "#e11d48"),
        ("Expedia", f"https://www.expedia.com/Hotel-Search?destination={dest_enc}", "#0c69b0"),
        ("Booking.com", f"https://www.booking.com/search.html?ss={dest_enc}", "#003580"),
    ]
    valid = [(name, url, color) for name, url, color in hotels if validate_url(url)]
    if not valid:
        return (
            f'<p style="font-size:13px;color:#64748b;margin:12px 0;text-align:center;">'
            f'Agoda · Expedia · Booking.com 등에서 {destination} 숙소를 검색하실 수 있습니다.</p>'
        )
    btns = "".join(
        f'<a href="{url}" target="_blank" rel="nofollow noopener" '
        f'style="{_BTN_STYLE}background:{color};">{name}에서 숙소 검색</a>'
        for name, url, color in valid
    )
    return f'<div style="{_BTN_WRAP}">{btns}</div>'


def build_hotel_buttons_custom(destination: str) -> str:
    """세시간전 제휴 링크 기반 맞춤형 숙소 CTA 버튼 (Agoda · Expedia · Trip.com)."""
    hotels = [
        (AFF_AGODA,   "#e11d48", f"{destination} 인기 숙소 시크릿 특가 및 남은 객실 확인하기 ▶"),
        (AFF_EXPEDIA, "#0c69b0", f"{destination} 추천 숙소 무료 취소 가능 객실 선점하기 ▶"),
        (AFF_TRIP,    "#1a7abf", f"{destination} Trip.com 최저가 호텔 바로 예약하기 ▶"),
    ]
    btns = "".join(
        f'<a href="{url}" target="_blank" rel="nofollow noopener sponsored" '
        f'style="{_BTN_STYLE}background:{color};width:100%;box-sizing:border-box;'
        f'text-align:center;display:block;margin:6px 0;">{label}</a>'
        for url, color, label in hotels
    )
    return f'<div style="margin:16px 0 8px 0;">{btns}</div>'


def _get_top_tour(destination: str, overview: str) -> str:
    """Gemini로 해당 여행지의 가장 인기 있는 필수 투어/액티비티 이름을 반환합니다."""
    try:
        prompt = (
            f"Destination: {destination}\n"
            f"Overview: {overview[:500]}\n\n"
            f"What is the single most popular must-do tour or activity for tourists visiting {destination}? "
            f"Reply with ONLY the tour/activity name in Korean (3–10 words). No explanation. "
            f"Examples: '사하라 사막 낙타 일몰 투어', '블루 라군 스노클링 투어', '장가계 케이블카 전망대 투어'"
        )
        model = genai.GenerativeModel("gemini-2.0-flash")
        resp = model.generate_content(prompt)
        name = resp.text.strip().strip("'\"")
        if 3 <= len(name) <= 40:
            return name
    except Exception as e:
        logger.debug(f"투어 이름 생성 실패: {e}")
    return f"{destination} 대표 투어"


def build_tour_buttons(destination: str, tour_name: str) -> str:
    """세시간전 제휴 링크 기반 Klook 투어 버튼 + Trip.com 액티비티 버튼."""
    entries = [
        (AFF_KLOOK, "#e85d04", f"Klook │ {tour_name} 최저가 예약하기 ▶"),
        (AFF_TRIP,  "#1a7abf", f"Trip.com │ {destination} 투어·액티비티 예약하기 ▶"),
    ]
    btns = "".join(
        f'<a href="{url}" target="_blank" rel="nofollow noopener sponsored" '
        f'style="{_BTN_STYLE}background:{color};width:100%;box-sizing:border-box;'
        f'text-align:center;display:block;margin:6px 0;">{label}</a>'
        for url, color, label in entries
    )
    return (
        f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;'
        f'padding:16px 20px;margin:20px 0;">'
        f'<p style="margin:0 0 10px 0;font-size:12px;font-weight:700;color:#166534;'
        f'letter-spacing:0.05em;">필수 투어 · 액티비티 예약</p>'
        f'{btns}</div>'
    )


# ==========================================
# 11. Gemini 콘텐츠 생성
# ==========================================

def build_prompt(data_famous: Dict, data_hidden: Dict, style_guide: str, continent: str = "") -> str:
    famous = data_famous["destination"]
    hidden = data_hidden["destination"]
    dest   = hidden  # 심층 탐구 대상 = 숨은 여행지
    maps_embed = (
        f'<iframe src="https://maps.google.com/maps?q={quote(hidden)}&output=embed" '
        f'width="100%" height="300" style="border:0;border-radius:12px;margin-top:12px;" '
        f'allowfullscreen="" loading="lazy"></iframe>'
    )
    coupang_block = "" if not COUPANG_LINK else (
        f'<div style="margin:32px 0;padding:24px 28px;background:#fff7ed;'
        f'border:1px solid #fed7aa;border-radius:16px;">'
        f'<p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#ea580c;letter-spacing:0.05em;">'
        f'{dest} 여행 준비물</p>'
        f'<p style="margin:0 0 16px 0;font-size:14px;color:#78350f;line-height:1.7;">'
        f'출발 전 챙겨야 할 필수 아이템을 한곳에서 확인할 수 있습니다. '
        f'캐리어·보조배터리·여행 파우치 등 여행에 꼭 필요한 준비물을 미리 점검하세요.</p>'
        f'<a href="{COUPANG_LINK}" target="_blank" rel="nofollow sponsored" '
        f'style="display:inline-block;background:#ea580c;color:#fff;font-size:14px;'
        f'font-weight:700;padding:10px 22px;border-radius:8px;text-decoration:none;">'
        f'여행 필수템 보러가기</a>'
        f'</div>'
    )
    coupang_disclosure = "" if not COUPANG_LINK else (
        '<p style="margin-top:24px;font-size:12px;color:#94a3b8;text-align:center;line-height:1.8;">'
        '이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.</p>'
    )
    continent_label = continent or "전 세계"

    return f"""
당신은 trip.bestwellth.org의 전문 여행 큐레이터입니다.
아래 [수집 정보]와 [가이드북 스타일]을 바탕으로 완성된 블로그 포스팅 HTML을 작성하세요.

[오늘의 여행지 컨텍스트]
오늘은 {continent_label} 특집입니다.
이 포스팅은 두 여행지를 연계하여 소개합니다.
- 유명 여행지: {famous} (많은 한국 여행자가 방문하는 곳 — 핵심만 간략히 소개)
- 연계 여행지: {hidden} (아직 잘 알려지지 않은 숨은 보석 — 심층 탐구)
포스팅의 핵심 가치: "{famous}를 방문한 김에 {hidden}까지 가보면 어떨까?" 라는 발견의 즐거움 제공

[가이드북 스타일 지침]
{style_guide}

[수집 정보]
★ 유명 여행지 (핵심 안내용): {famous}
  개요: {data_famous["overview"][:1500]}
  주요 명소 요약: {data_famous["attractions"][:1500]}
  교통: {data_famous["transport"][:600]}

★ 연계 여행지 (심층 탐구 대상): {hidden}
  개요: {data_hidden["overview"][:2000]}
  명소: {data_hidden["attractions"][:2500]}
  맛집: {data_hidden["food"][:1200]}
  교통 ({famous}→{hidden} 이동 포함): {data_hidden["transport"][:800]}
  숙소: {data_hidden["accommodation"][:800]}
  여행팁: {data_hidden["tips"][:600]}
  참고 출처: {', '.join(data_hidden["sources"])}

[절대 금지 사항]
- 이모티콘(Emoji) 사용 전면 금지 (제목·본문 모두)
- 개인 일기·경험 형식 금지 ("저는", "제가", "다녀왔습니다" 등)
- Markdown 기호(**, ##, -, *) 본문 삽입 금지
- 수치·사실 지어내기 금지
- "~것으로 보인다", "~것으로 추정된다" 류 모호한 표현 금지
- 본문에 외부 링크(href 포함 a태그) 직접 삽입 금지 — 버튼·링크는 {{TICKET_BUTTONS}} {{TRANSPORT_BUTTONS}} {{HOTEL_BUTTONS}} 플레이스홀더가 자동 처리함
- "바로가기", "웹사이트 링크", "예매하기" 등 링크성 텍스트를 본문 p태그 안에 삽입 금지

[문체]
- 문어체 (이다, 한다, 위치한다, 운영된다)
- 객관적이고 전문적인 여행 가이드북 큐레이션

[픽토그램 플레이스홀더 — 각 섹션 h2 바로 위에 삽입]
{{PICTOGRAM:attraction}} {{PICTOGRAM:food}} {{PICTOGRAM:transport}} {{PICTOGRAM:accommodation}} {{PICTOGRAM:tips}}

[티켓·예약 URL 지침]
- 관광지에 별도 예약 사이트(공식·GetYourGuide·Klook·Viator)가 확실히 존재하면 [TICKET_URLS]에 포함
- 패스트트랙(Fast Track / Skip-the-Line) 옵션이 별도로 존재하면 "[명소명] 패스트트랙|URL" 형태로 추가 항목 작성
- 현지 대중교통·특수 교통(특급열차·케이블카·페리 등) 공식 티켓 사이트가 있으면 [TRANSPORT_URLS]에 포함
- 불확실하거나 추측한 URL은 절대 작성 금지 (누락이 오류보다 낫다)
- URL은 반드시 https://로 시작하는 완전한 형태로 작성

[섹션 사진 플레이스홀더 — h2 아래 p태그 시작 전에 그대로 출력, 실제 사진으로 교체됨]
{{PHOTO:attraction}} {{PHOTO:food}} {{PHOTO:transport}} {{PHOTO:tips}}

[삽입 요소]
구글 지도 iframe (명소 섹션 바로 아래): {maps_embed}

[HTML 구조 — 반드시 이 순서로]

카테고리 색상: {CAT_COLOR} | 라이트 배경: {CAT_LIGHT_BG} | 라이트 테두리: {CAT_LIGHT_BORDER} | 다크: {CAT_DARK}

--- 1. 카테고리 뱃지 ---
<div style="display:inline-block;background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:13px;font-weight:700;padding:4px 14px;border-radius:20px;margin-bottom:14px;">여행 가이드 · {dest}</div>

--- 2. 서브 제목 (H1 금지, div 사용) ---
<div style="font-size:clamp(20px,4vw,26px);font-weight:800;color:#0f172a;margin:0 0 8px 0;line-height:1.4;">[핵심 한 줄 서브 문구 — "전 세계가 주목하는" 류 표현 자연스럽게 포함]</div>

--- 3. 인트로 박스 ---
<div style="background:#f8fafc;padding:28px 30px;border-radius:16px;border:1px solid #e2e8f0;margin-bottom:40px;">
  <p style="margin-top:0;font-size:13px;font-weight:700;color:#94a3b8;letter-spacing:0.08em;margin-bottom:16px;">이 글에서 다루는 내용</p>
  <ul style="list-style:none !important;padding:0 !important;margin:0 0 24px 0 !important;">
    <li style="display:flex;align-items:flex-start;gap:12px;font-size:15px;color:#334155;line-height:1.8;margin-bottom:10px;list-style:none;"><span style="display:inline-block;width:6px;height:6px;min-width:6px;background:{CAT_COLOR};border-radius:50%;margin-top:9px;flex-shrink:0;"></span><span style="flex:1;">[항목 1 — 포커스 키워드 포함]</span></li>
    <li style="display:flex;align-items:flex-start;gap:12px;font-size:15px;color:#334155;line-height:1.8;margin-bottom:10px;list-style:none;"><span style="display:inline-block;width:6px;height:6px;min-width:6px;background:{CAT_COLOR};border-radius:50%;margin-top:9px;flex-shrink:0;"></span><span style="flex:1;">[항목 2]</span></li>
    <li style="display:flex;align-items:flex-start;gap:12px;font-size:15px;color:#334155;line-height:1.8;list-style:none;"><span style="display:inline-block;width:6px;height:6px;min-width:6px;background:{CAT_COLOR};border-radius:50%;margin-top:9px;flex-shrink:0;"></span><span style="flex:1;">[항목 3]</span></li>
  </ul>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:0 0 20px 0;">
  <div style="display:flex;flex-wrap:wrap;gap:8px;">
    <span style="background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:12px;font-weight:600;padding:4px 12px;border-radius:20px;">#[키워드1]</span>
    <span style="background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:12px;font-weight:600;padding:4px 12px;border-radius:20px;">#[키워드2]</span>
    <span style="background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:12px;font-weight:600;padding:4px 12px;border-radius:20px;">#[키워드3]</span>
    <span style="background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:12px;font-weight:600;padding:4px 12px;border-radius:20px;">#[키워드4]</span>
  </div>
</div>

--- 3.5. 여행 기본 정보 표 (인트로 박스 바로 아래) ---
<table style="width:100%;border-collapse:collapse;margin:0 0 32px 0;font-size:14px;">
  <thead><tr style="background:#0c4a6e;color:#fff;">
    <th style="padding:12px 16px;text-align:center;font-weight:700;width:35%;">항목</th>
    <th style="padding:12px 16px;text-align:center;font-weight:700;">내용</th>
  </tr></thead>
  <tbody>
    <tr style="background:#fff;border-bottom:1px solid #e2e8f0;"><td style="padding:11px 16px;font-weight:700;color:#0f172a;text-align:center;">위치</td><td style="padding:11px 16px;color:#334155;">[국가 · 지역(주/도) · 도시 또는 마을명 순으로. 예: 캐나다 브리티시컬럼비아주 · 하이다 과이 제도]</td></tr>
    <tr style="background:#f8fafc;border-bottom:1px solid #e2e8f0;"><td style="padding:11px 16px;font-weight:700;color:#0f172a;text-align:center;">최적 여행 시기</td><td style="padding:11px 16px;color:#334155;">[시기 + 한 줄 이유]</td></tr>
    <tr style="background:#fff;border-bottom:1px solid #e2e8f0;"><td style="padding:11px 16px;font-weight:700;color:#0f172a;text-align:center;">언어</td><td style="padding:11px 16px;color:#334155;">[공용어]</td></tr>
    <tr style="background:#f8fafc;border-bottom:1px solid #e2e8f0;"><td style="padding:11px 16px;font-weight:700;color:#0f172a;text-align:center;">통화</td><td style="padding:11px 16px;color:#334155;">[통화명 및 기호]</td></tr>
    <tr style="background:#fff;border-bottom:1px solid #e2e8f0;"><td style="padding:11px 16px;font-weight:700;color:#0f172a;text-align:center;">시차 (한국 기준)</td><td style="padding:11px 16px;color:#334155;">[UTC±X / 한국보다 N시간]</td></tr>
    <tr style="background:#f8fafc;"><td style="padding:11px 16px;font-weight:700;color:#0f172a;text-align:center;">1일 평균 예산</td><td style="padding:11px 16px;color:#334155;">[예산 범위 USD]</td></tr>
  </tbody>
</table>

--- 4. 핵심 요약 카드 ---
<div style="background:#0c4a6e;border-radius:16px;padding:28px 30px;margin-bottom:32px;">
  <p style="margin:0 0 16px 0;font-size:15px;font-weight:700;color:#ffffff;letter-spacing:0.08em;">핵심 3가지</p>
  <ul style="list-style:none;padding:0;margin:0;">
    <li style="display:flex;align-items:flex-start;gap:12px;margin-bottom:12px;"><span style="display:inline-block;background:{CAT_COLOR};color:#fff;font-size:12px;font-weight:800;padding:2px 8px;border-radius:4px;flex-shrink:0;margin-top:2px;">01</span><span style="font-size:15px;color:#ffffff;line-height:1.7;">[핵심 포인트 1]</span></li>
    <li style="display:flex;align-items:flex-start;gap:12px;margin-bottom:12px;"><span style="display:inline-block;background:{CAT_COLOR};color:#fff;font-size:12px;font-weight:800;padding:2px 8px;border-radius:4px;flex-shrink:0;margin-top:2px;">02</span><span style="font-size:15px;color:#ffffff;line-height:1.7;">[핵심 포인트 2]</span></li>
    <li style="display:flex;align-items:flex-start;gap:12px;"><span style="display:inline-block;background:{CAT_COLOR};color:#fff;font-size:12px;font-weight:800;padding:2px 8px;border-radius:4px;flex-shrink:0;margin-top:2px;">03</span><span style="font-size:15px;color:#ffffff;line-height:1.7;">[핵심 포인트 3]</span></li>
  </ul>
</div>

--- 5. 추천 / 비추 섹션 ---
<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:32px;">
  <div style="flex:1;min-width:220px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:14px;padding:20px 22px;">
    <p style="margin:0 0 12px 0;font-size:13px;font-weight:800;color:#166534;letter-spacing:0.05em;">이런 분께 추천</p>
    <ul style="margin:0;padding-left:16px;font-size:14px;color:#166534;line-height:2.0;">
      <li>[추천 여행자 유형 1 — 구체적으로]</li>
      <li>[추천 여행자 유형 2]</li>
      <li>[추천 여행자 유형 3]</li>
    </ul>
  </div>
  <div style="flex:1;min-width:220px;background:#fff1f2;border:1px solid #fecdd3;border-radius:14px;padding:20px 22px;">
    <p style="margin:0 0 12px 0;font-size:13px;font-weight:800;color:#9f1239;letter-spacing:0.05em;">이런 분께 비추</p>
    <ul style="margin:0;padding-left:16px;font-size:14px;color:#9f1239;line-height:2.0;">
      <li>[비추 여행자 유형 1 — 구체적으로, 솔직하게]</li>
      <li>[비추 여행자 유형 2]</li>
      <li>[비추 여행자 유형 3]</li>
    </ul>
  </div>
</div>

--- 6. 구글 지도 섹션 ---
<div style="margin-bottom:36px;"><p style="font-size:14px;font-weight:700;color:#334155;margin-bottom:8px;">{dest} 위치</p>{maps_embed}</div>

[[[AD_DISPLAY]]]

--- 6. 본문 (PART 1 → 연결 브릿지 → PART 2 → 동선 → 맛집/교통/숙소/팁) ---
H2 번호 금지. 포커스 키워드는 H2 전체에서 최대 1회.
강조: <span style="background-color:{CAT_LIGHT_BG};padding:2px 6px;color:{CAT_COLOR};font-weight:700;">강조 텍스트</span>

--- PART 1. 유명 여행지 핵심 안내 ---
<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  {{PICTOGRAM:attraction}}
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">{famous} — 떠나기 전 꼭 알아야 할 것들</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[{famous} 한 줄 매력 포인트]</p>
  {{PHOTO:famous}}

  [도입 p태그 1~2개 — {famous}의 전반적 매력과 방문 가치를 간결하게 서술]

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">{famous}에서 놓치면 안 될 핵심 명소 3선</h3>
  <p>[명소A (영문명): 위치·특징·실용정보 — 1~2줄로 간결하게]</p>
  <p>[명소B (영문명): 위치·특징·실용정보]</p>
  <p>[명소C (영문명): 위치·특징·실용정보]</p>

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">{famous} 기본 이동 정보</h3>
  [입국 및 시내 이동 p태그 1~2개 — 공항명, 시내 이동 수단 간략 안내]
</div>

--- 연결 브릿지 ---
<div style="background:{CAT_LIGHT_BG};border:1px solid {CAT_LIGHT_BORDER};border-radius:16px;padding:24px 28px;margin:0 0 48px 0;">
  <p style="margin:0 0 8px 0;font-size:13px;font-weight:800;color:{CAT_COLOR};letter-spacing:0.05em;">{famous}에서 한 발 더</p>
  <p style="margin:0;font-size:15px;color:#334155;line-height:1.8;">[{famous}에서 {hidden}까지 이동 방법·소요 시간·비용을 구체적으로 서술. "버스로 약 X시간" 또는 "차량으로 X분" 형태로 명시.]</p>
</div>

[[[AD_IN_ARTICLE]]]

--- PART 2. 연계 여행지 심층 탐구 ---
<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">{hidden} — 아직 많은 이들이 모르는 곳</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[{hidden} 한 줄 핵심 매력]</p>

  [도입 p태그 1~2개 — {hidden}의 특별함과 방문 가치 서술]

  아래 3개 테마로 명소를 분류하여 H3 소제목 + 세부 명소 설명으로 구성한다.
  각 명소는 반드시 한국어명(영문명) 병기. 위치(지역·마을명), 핵심 볼거리, 접근 방법·허가 필요 여부·소요 시간 등 실용 정보를 p태그에 포함한다.
  명소는 전체 합산 최소 6개 이상 언급한다.

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">[테마1 — 예: 문화·역사 유산]</h3>
  <p>[명소A (영문명): 위치·특징·실용정보.]</p>
  <p>[명소B (영문명): 위치·특징·실용정보.]</p>
  <p>[명소C (영문명): 위치·특징·실용정보.]</p>
  {{PHOTO:attraction}}

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">[테마2 — 예: 자연·하이킹·국립공원]</h3>
  <p>[명소D (영문명): 난이도·거리·소요시간·출발지 포함.]</p>
  <p>[명소E (영문명): 명소 설명·실용정보.]</p>
  <p>[명소F (영문명): 명소 설명·실용정보.]</p>
  {{PHOTO:attraction2}}

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">[테마3 — 예: 해변·체험·액티비티 또는 로컬 명소]</h3>
  <p>[명소G (영문명): 명소 설명·실용정보.]</p>
  <p>[명소H (영문명): 명소 설명·실용정보.]</p>
  {{PHOTO:attraction3}}

  {{PINTEREST_IMAGES}}

  <div style="background:{CAT_LIGHT_BG};border-left:4px solid {CAT_COLOR};padding:16px 20px;border-radius:0 12px 12px 0;margin:24px 0 0 0;">
    <p style="margin:0 0 8px 0;font-size:12px;font-weight:700;color:{CAT_COLOR};letter-spacing:0.05em;">방문 전 필수 체크</p>
    <ul style="margin:0;padding-left:18px;font-size:14px;color:#334155;line-height:1.9;">
      <li>[명소 방문 시 주의사항 또는 예약 필수 여부]</li>
      <li>[계절·날씨에 따른 방문 팁]</li>
      <li>[현지 가이드·투어 추천 여부]</li>
    </ul>
  </div>
  {{ATTRACTION_TICKET_TABLE}}
  {{TICKET_BUTTONS}}
  {{TOUR_BUTTONS}}
</div>

--- 추천 동선 ---
<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">{famous} + {hidden} 함께하는 추천 일정</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[일정 한 줄 요약]</p>
  [N박M일 추천 동선을 p태그 또는 간단한 표로 제시. 예: 1~2일차 {famous} → 3~4일차 {hidden} → 귀국. 이동 방법·숙박지 간략 포함.]
</div>

[[[AD_IN_ARTICLE]]]

<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  {{PICTOGRAM:food}}
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">[맛집 제목]</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[서브 문구]</p>
  {{PHOTO:food}}
  [본문 p태그 3~5개 — 서술형, 대표 음식·식당명 포함]
  <div style="background:{CAT_LIGHT_BG};border-left:4px solid {CAT_COLOR};padding:16px 20px;border-radius:0 12px 12px 0;margin:24px 0 0 0;">
    <p style="margin:0 0 8px 0;font-size:12px;font-weight:700;color:{CAT_COLOR};letter-spacing:0.05em;">핵심 포인트</p>
    <ul style="margin:0;padding-left:18px;font-size:14px;color:#334155;line-height:1.9;">
      <li>[맛집 핵심 1]</li>
      <li>[맛집 핵심 2]</li>
      <li>[맛집 핵심 3]</li>
    </ul>
  </div>
</div>

<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  {{PICTOGRAM:transport}}
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">[교통 제목]</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[서브 문구]</p>
  {{PHOTO:transport}}

  [{famous} 관문 공항 안내 p태그 1개 + {famous}→{hidden} 이동 방법 안내 p태그 1개]
  [반드시 포함: {famous}에 입국할 때 이용하는 공항명과 공항코드를 명시한다.
   이후 {hidden}까지 이동하는 주요 교통수단(버스·차량·기차 등)과 소요 시간을 함께 서술한다.]

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">공항에서 시내·목적지까지</h3>
  [공항→목적지 이동 안내 p태그 1~2개 — 도착 공항에서 시내까지 거리·이동 방법 개요]
  <table style="width:100%;border-collapse:collapse;margin:16px 0 20px;font-size:14px;">
    <thead><tr style="background:#0c4a6e;color:#fff;">
      <th style="padding:10px 14px;text-align:center;">교통수단</th>
      <th style="padding:10px 14px;text-align:center;">소요 시간</th>
      <th style="padding:10px 14px;text-align:center;">비용</th>
      <th style="padding:10px 14px;text-align:center;">특징 및 주의사항</th>
    </tr></thead>
    <tbody>
      [공항→시내 이동수단별 tr 3~4행 (현지 실정에 맞게). 각 행 첫 번째 td는 text-align:center 적용]
    </tbody>
  </table>

  {{TRANSPORT_CLASSES_TABLE}}
  {{TRANSPORT_BUTTONS}}

  {{PICTOGRAM:accommodation}}
  <h3 style="font-size:clamp(16px,2.5vw,19px);font-weight:700;color:#0f172a;margin:8px 0 16px 0;">[숙소 소제목]</h3>
  [숙소 본문 p태그 2~3개 — 지역별 특성·추천 숙박 지구 서술]
  {{ACCOMMODATION_TABLE}}
  {{HOTEL_BUTTONS}}

  <div style="background:{CAT_LIGHT_BG};border-left:4px solid {CAT_COLOR};padding:16px 20px;border-radius:0 12px 12px 0;margin:24px 0 0 0;">
    <p style="margin:0 0 8px 0;font-size:12px;font-weight:700;color:{CAT_COLOR};letter-spacing:0.05em;">교통·숙소 핵심 포인트</p>
    <ul style="margin:0;padding-left:18px;font-size:14px;color:#334155;line-height:1.9;">
      <li>[교통 핵심 — 공항→시내 최적 수단]</li>
      <li>[숙소 핵심 — 추천 지구 또는 숙박 팁]</li>
      <li>[예약 팁 — 성수기 주의·예약 시점 등]</li>
    </ul>
  </div>
</div>

<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  {{PICTOGRAM:tips}}
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">[여행팁 제목]</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[서브 문구]</p>
  {{PHOTO:tips}}
  [본문 p태그 3~5개 — 서술형]
  <div style="background:{CAT_LIGHT_BG};border-left:4px solid {CAT_COLOR};padding:16px 20px;border-radius:0 12px 12px 0;margin:24px 0 0 0;">
    <p style="margin:0 0 8px 0;font-size:12px;font-weight:700;color:{CAT_COLOR};letter-spacing:0.05em;">여행 전 체크리스트</p>
    <ul style="margin:0;padding-left:18px;font-size:14px;color:#334155;line-height:1.9;">
      <li>[팁 1]</li>
      <li>[팁 2]</li>
      <li>[팁 3]</li>
      <li>[팁 4]</li>
    </ul>
  </div>
</div>

--- 6.5 여행 준비물 (쿠팡 파트너스) ---
{coupang_block}

--- 7. 3카드 요약 ---
<div style="margin-top:60px;padding-top:40px;border-top:2px dashed #cbd5e1;">
  <h3 style="text-align:center;color:#0f172a;margin-bottom:24px;font-size:20px;font-weight:800;">한눈에 보는 핵심 요약</h3>
  <div style="display:flex;flex-wrap:wrap;gap:14px;padding-bottom:12px;">
    <div style="flex:1;min-width:200px;background:{CAT_LIGHT_BG};border:1px solid {CAT_LIGHT_BORDER};padding:20px;border-radius:18px;text-align:center;"><p style="margin:0;font-weight:800;color:{CAT_COLOR};font-size:15px;margin-bottom:8px;">[카드1 제목]</p><p style="margin:0;font-size:14px;color:#334155;line-height:1.6;">[카드1 내용]</p></div>
    <div style="flex:1;min-width:200px;background:{CAT_LIGHT_BG};border:1px solid {CAT_LIGHT_BORDER};padding:20px;border-radius:18px;text-align:center;"><p style="margin:0;font-weight:800;color:{CAT_COLOR};font-size:15px;margin-bottom:8px;">[카드2 제목]</p><p style="margin:0;font-size:14px;color:#334155;line-height:1.6;">[카드2 내용]</p></div>
    <div style="flex:1;min-width:200px;background:{CAT_LIGHT_BG};border:1px solid {CAT_LIGHT_BORDER};padding:20px;border-radius:18px;text-align:center;"><p style="margin:0;font-weight:800;color:{CAT_COLOR};font-size:15px;margin-bottom:8px;">[카드3 제목]</p><p style="margin:0;font-size:14px;color:#334155;line-height:1.6;">[카드3 내용]</p></div>
  </div>
</div>

--- 8. 참고 자료 ---
<div style="margin-top:48px;padding:24px;background:#f8fafc;border-radius:12px;border:1px solid #e2e8f0;">
  <h4 style="margin:0 0 14px 0;color:#334155;font-size:16px;font-weight:700;">참고 자료</h4>
  <ul style="list-style:none;padding:0;margin:0;font-size:14px;color:#334155;line-height:2.2;">[출처 li 태그]</ul>
</div>

[[[AD_AUTORELAXED]]]

--- 9. 면책 조항 ---
<div style="margin-top:2em;padding:20px 24px;background:#fafafa;border-radius:12px;border:1px solid #e2e8f0;">
  <p style="margin:0 0 8px 0;font-size:13px;font-weight:700;color:#64748b;">여행 유의사항</p>
  <p style="margin:0;font-size:13px;color:#94a3b8;line-height:1.8;">본 콘텐츠는 여행 정보 제공을 목적으로 작성되었으며, 실제 운영 시간·입장료·교통편 등은 현지 상황에 따라 변경될 수 있습니다. 방문 전 공식 채널을 통해 최신 정보를 반드시 확인하시기 바랍니다.</p>
</div>

--- 10. 쿠팡 파트너스 고지 (최하단) ---
{coupang_disclosure}

[응답 형식 — 맨 끝에 순서대로 출력]
[TITLE]
아래 규칙으로 제목을 작성하세요.
- 두 여행지명은 반드시 한국어로 번역하여 사용 (영어 지명 금지. 예: Chiang Mai → 치앙마이, Pai → 파이, Kyoto → 교토)
- 두 여행지를 모두 자연스럽게 담은 감성적 한국어 문장
- "여행 완전 정복", "총정리", "가이드" 같은 정보성 표현 금지
- "치앙마이가 알려준 파이", "교토에서 한 발 더, 아마노하시다테", "마라케시 너머의 아이트 벤 하두" 형태 권장
- 30자 이내로 간결하게
[/TITLE]
[COUNTRY_KR]{famous}가 속한 국가명을 한국어로 (최대 6자, 예: 태국, 모로코, 뉴질랜드)[/COUNTRY_KR]
[FOCUS_KW]3~4단어 한국어 롱테일 키워드[/FOCUS_KW]
[META_DESC]130~155자 메타 설명[/META_DESC]
[SLUG]{famous}와 {hidden} 두 여행지명 모두 포함한 3~6단어 영문 하이픈 슬러그 (예: chiang-mai-pai-hidden-gem)[/SLUG]
[EXCERPT]100~150자 발췌문[/EXCERPT]
[HOTELS]
숙소명|유형(hotel/cruise/resort/hostel/liveaboard/ryokan 등)
[/HOTELS]
[ATTRACTIONS]
관광지명
[/ATTRACTIONS]
[TRANSPORT_SERVICES]
교통편명|유형(flight/train/ferry/cruise/cable_car/bus 등)
[/TRANSPORT_SERVICES]
[TICKET_URLS]
관광지명|공식예약URL
(예약 필요한 명소만 포함. 없으면 이 사이 내용을 비워두고 태그는 유지)
[/TICKET_URLS]
[TRANSPORT_URLS]
교통수단명|공식예약URL
(현지 교통 공식 티켓 사이트가 있는 경우만. 없으면 이 사이 내용을 비워두고 태그는 유지)
[/TRANSPORT_URLS]
"""


def generate_content(data_famous: Dict, data_hidden: Dict, style_guide: str, continent: str = "") -> Dict:
    with tracer.start_as_current_span("generate_content") as span:
        span.set_attribute("destination", data_hidden["destination"])
        span.set_attribute("famous", data_famous["destination"])
        prompt = build_prompt(data_famous, data_hidden, style_guide, continent)
        for attempt in range(3):
            try:
                resp = gemini.generate_content(prompt)
                raw  = resp.text
                logger.info(f"Gemini 콘텐츠 생성 완료 ({len(raw)}자)")
                return _parse(raw, data_hidden, data_famous["destination"])
            except Exception as e:
                logger.warning(f"Gemini 호출 실패 ({attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(15 * (attempt + 1))
                else:
                    raise


def _parse(raw: str, data: Dict, famous: str = "") -> Dict:
    def ex(tag: str, default: str = "") -> str:
        m = re.search(rf'\[{tag}\](.*?)\[/{tag}\]', raw, re.DOTALL)
        return m.group(1).strip() if m else default

    def parse_url_block(tag: str) -> List[tuple]:
        m = re.search(rf'\[{tag}\](.*?)\[/{tag}\]', raw, re.DOTALL)
        if not m:
            return []
        entries = []
        for line in m.group(1).strip().splitlines():
            line = line.strip()
            if '|' in line:
                name, url = line.split('|', 1)
                name, url = name.strip(), url.strip()
                if name and url.startswith('http'):
                    entries.append((name, url))
        return entries

    def parse_list_block(tag: str) -> List[str]:
        m = re.search(rf'\[{tag}\](.*?)\[/{tag}\]', raw, re.DOTALL)
        if not m:
            return []
        return [l.strip() for l in m.group(1).strip().splitlines() if l.strip() and not l.strip().startswith('(')]

    dest = data["destination"]
    body = raw
    for tag in ["TITLE", "FOCUS_KW", "META_DESC", "SLUG", "EXCERPT",
                "HOTELS", "ATTRACTIONS", "TRANSPORT_SERVICES",
                "TICKET_URLS", "TRANSPORT_URLS"]:
        body = re.sub(rf'\[{tag}\].*?\[/{tag}\]\n?', '', body, flags=re.DOTALL)
    # Gemini가 ```html ... ``` 코드블록으로 감싸는 경우 제거
    body = re.sub(r'^```(?:html)?\s*\n?', '', body.strip(), flags=re.IGNORECASE)
    body = re.sub(r'\n?```\s*$', '', body, flags=re.IGNORECASE)
    body = body.strip()

    for key in _PICTOGRAMS:
        body = body.replace(f'{{PICTOGRAM:{key}}}', pictogram_html(key))
    body = re.sub(r'<a(?![^>]*\brel=)[^>]*>(.*?)</a>', r'\1', body, flags=re.DOTALL)  # Gemini 생성 링크만 제거, rel= 있는 버튼은 유지

    # 광고 플레이스홀더 교체: 지도 아래, 본문 1 아래, 참고자료 아래
    body = body.replace('[[[AD_DISPLAY]]]', AD_DISPLAY)
    body = body.replace('[[[AD_IN_ARTICLE]]]', AD_IN_ARTICLE)
    body = body.replace('[[[AD_AUTORELAXED]]]', AD_AUTORELAXED)

    raw_title   = ex("TITLE",      f"{dest} 여행 가이드 — 명소·맛집·교통 총정리")
    country_kr  = ex("COUNTRY_KR", "").strip()
    full_title  = f"[{country_kr}] {raw_title}" if country_kr else raw_title

    return {
        "destination":  dest,
        "famous":       famous,
        "title":        full_title,
        "country_kr":   country_kr,
        "focus_kw":     ex("FOCUS_KW",  f"{dest} 여행 가이드"),
        "meta_desc":    ex("META_DESC", f"{dest} 여행의 모든 것. 주요 명소, 현지 맛집, 교통을 한 곳에 확인하세요."),
        "slug":         ex("SLUG",      f"{dest.lower().replace(' ', '-')}-travel-guide"),
        "excerpt":      ex("EXCERPT",   ""),
        "ticket_urls":       parse_url_block("TICKET_URLS"),
        "transport_urls":    parse_url_block("TRANSPORT_URLS"),
        "hotels":            parse_list_block("HOTELS"),
        "attractions":       parse_list_block("ATTRACTIONS"),
        "transport_services": parse_list_block("TRANSPORT_SERVICES"),
        "body":              body,
    }


# ==========================================
# 12. 여행지 지역 분류
# ==========================================

_REGION_KEYWORDS = {
    "Asia": [
        "japan", "korea", "china", "india", "thailand", "vietnam", "bali", "indonesia",
        "singapore", "hong kong", "taiwan", "philippines", "myanmar", "cambodia", "laos",
        "malaysia", "nepal", "sri lanka", "maldives", "dubai", "abu dhabi", "qatar",
        "istanbul", "turkey", "jordan", "israel", "georgia", "armenia", "azerbaijan",
        "tokyo", "kyoto", "osaka", "seoul", "bangkok", "beijing", "shanghai", "delhi",
        "mumbai", "hanoi", "ho chi minh", "yangon", "phuket", "chiang mai",
    ],
    "Europe": [
        "paris", "london", "rome", "barcelona", "amsterdam", "prague", "vienna", "berlin",
        "lisbon", "porto", "madrid", "florence", "venice", "athens", "santorini", "mykonos",
        "budapest", "warsaw", "stockholm", "oslo", "copenhagen", "helsinki", "dublin",
        "edinburgh", "brussels", "luxembourg", "zurich", "geneva", "milan", "naples",
        "amalfi", "cinque terre", "dubrovnik", "split", "kotor", "iceland", "reykjavik",
        "tallinn", "riga", "vilnius", "krakow", "salzburg", "innsbruck", "monaco",
        "france", "spain", "italy", "germany", "portugal", "greece", "netherlands",
        "sweden", "norway", "denmark", "finland", "ireland", "scotland", "switzerland",
        "austria", "croatia", "slovenia", "czech", "poland", "hungary", "romania",
        "bulgaria", "serbia", "montenegro", "albania", "north macedonia", "slovakia",
        "tuscany", "provence", "andalusia", "algarve", "sicily", "sardinia",
    ],
    "North America": [
        "new york", "los angeles", "chicago", "miami", "las vegas", "san francisco",
        "seattle", "boston", "washington", "toronto", "vancouver", "montreal", "quebec",
        "cancun", "mexico city", "guadalajara", "havana", "cuba", "jamaica", "bahamas",
        "costa rica", "panama", "belize", "guatemala", "honduras", "nicaragua",
        "hawaii", "alaska", "yellowstone", "grand canyon", "yosemite", "banff",
        "usa", "canada", "mexico",
    ],
    "Oceania": [
        "sydney", "melbourne", "brisbane", "perth", "adelaide", "cairns", "gold coast",
        "auckland", "queenstown", "wellington", "christchurch", "rotorua",
        "fiji", "bora bora", "tahiti", "samoa", "tonga", "vanuatu", "papua new guinea",
        "australia", "new zealand",
    ],
    "South America": [
        "rio de janeiro", "sao paulo", "buenos aires", "lima", "santiago", "bogota",
        "cartagena", "medellin", "cusco", "machu picchu", "quito", "montevideo",
        "la paz", "sucre", "asuncion", "caracas", "guyana", "suriname",
        "patagonia", "atacama", "galapagos", "amazon", "iguazu",
        "brazil", "argentina", "peru", "colombia", "chile", "ecuador", "bolivia",
        "uruguay", "paraguay", "venezuela",
    ],
    "Africa": [
        "cairo", "marrakech", "casablanca", "nairobi", "cape town", "johannesburg",
        "zanzibar", "dar es salaam", "addis ababa", "accra", "lagos", "dakar",
        "tunis", "algiers", "tripoli", "khartoum", "kampala", "kigali", "lusaka",
        "harare", "maputo", "antananarivo", "victoria", "mauritius", "reunion",
        "seychelles", "comoros",
        "morocco", "egypt", "kenya", "tanzania", "south africa", "ethiopia",
        "ghana", "nigeria", "senegal", "ivory coast", "cameroon", "rwanda", "uganda",
        "zambia", "zimbabwe", "mozambique", "madagascar",
    ],
}


def classify_region(destination: str) -> str:
    """여행지를 7개 지역 카테고리 중 하나로 분류."""
    dest_lower = destination.lower()
    for region, keywords in _REGION_KEYWORDS.items():
        if any(k in dest_lower for k in keywords):
            return region

    # Gemini 분류 (키워드 매칭 실패 시)
    prompt = (
        f"Which geographic region does the travel destination '{destination}' belong to?\n"
        "Reply with exactly one of these words only (no other text):\n"
        "Africa | Asia | Europe | North America | Oceania | South America | Special Destinations\n"
        "'Special Destinations' is for Antarctica, remote islands, cruise ports, or places "
        "difficult to classify into a standard continent."
    )
    try:
        resp = gemini.generate_content(prompt)
        text = resp.text.strip()
        valid = ["Africa", "Asia", "Europe", "North America", "Oceania", "South America", "Special Destinations"]
        for v in valid:
            if v.lower() in text.lower():
                logger.info(f"Gemini 지역 분류: {destination} → {v}")
                return v
    except Exception as e:
        logger.warning(f"지역 분류 실패: {e}")

    return "Special Destinations"


# ==========================================
# 13. WordPress REST API  (구 12)
# ==========================================

def _wp_auth() -> Dict:
    token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def wp_upload_image(img_bytes: bytes, filename: str, alt: str = "") -> Optional[Dict]:
    with tracer.start_as_current_span("wp_upload_image"):
        try:
            ext = "jpeg" if filename.endswith(".jpg") else "png"
            headers = {
                "Authorization": _wp_auth()["Authorization"],
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": f"image/{ext}",
            }
            r = requests.post(f"{WP_SITE_URL}/wp-json/wp/v2/media",
                              headers=headers, data=img_bytes, timeout=90)
            r.raise_for_status()
            rj = r.json()
            media_id = rj.get("id")
            source_url = rj.get("source_url", "")
            if alt and media_id:
                requests.post(f"{WP_SITE_URL}/wp-json/wp/v2/media/{media_id}",
                              headers=_wp_auth(), json={"alt_text": alt}, timeout=15)
            logger.info(f"WP 이미지 업로드: media_id={media_id}, url={source_url}")
            return {"id": media_id, "url": source_url}
        except Exception as e:
            logger.error(f"WP 이미지 업로드 실패: {e}")
            return None


def wp_get_or_create_category(name: str) -> Optional[int]:
    try:
        r = requests.get(f"{WP_SITE_URL}/wp-json/wp/v2/categories",
                         headers=_wp_auth(), params={"search": name, "per_page": 5}, timeout=15)
        r.raise_for_status()
        cats = r.json()
        if cats:
            return cats[0]["id"]
        r2 = requests.post(f"{WP_SITE_URL}/wp-json/wp/v2/categories", headers=_wp_auth(),
                           json={"name": name, "slug": name.lower().replace(" ", "-")}, timeout=15)
        r2.raise_for_status()
        return r2.json().get("id")
    except Exception as e:
        logger.warning(f"카테고리 처리 실패: {e}")
        return None


def wp_get_published_destinations() -> set:
    """이미 발행된 포스트의 슬러그·제목을 수집해 중복 여행지 탐지에 사용합니다."""
    collected: set = set()
    page = 1
    while True:
        try:
            r = requests.get(
                f"{WP_SITE_URL}/wp-json/wp/v2/posts",
                headers=_wp_auth(),
                params={"per_page": 100, "page": page, "status": "publish", "_fields": "slug,title"},
                timeout=15,
            )
            if r.status_code in (400, 404):
                break
            r.raise_for_status()
            posts = r.json()
            if not posts:
                break
            for post in posts:
                slug = post.get("slug", "").lower()
                title_raw = post.get("title", {})
                title = (title_raw.get("rendered", "") if isinstance(title_raw, dict) else str(title_raw)).lower()
                if slug:
                    collected.add(slug)
                if title:
                    collected.add(title)
            if len(posts) < 100:
                break
            page += 1
        except Exception as e:
            logger.warning(f"발행 목록 조회 실패 (page {page}): {e}")
            break
    logger.info(f"기발행 포스트 {len(collected)}건 수집 완료 ({page}페이지)")
    return collected


def is_already_published(destination: str, published: set) -> bool:
    """여행지 이름이 기발행 슬러그/제목에 포함되어 있는지 확인합니다."""
    dest_lower = destination.lower()
    dest_slug  = dest_lower.replace(" ", "-")
    return any(dest_lower in item or dest_slug in item for item in published)


def wp_publish(content: Dict, media_id: Optional[int], cat_id: Optional[int]) -> Dict:
    with tracer.start_as_current_span("wp_publish") as span:
        payload = {
            "title":   content["title"],
            "content": content["body"],
            "excerpt": content["excerpt"],
            "status":  "draft",
            "slug":    content["slug"],
            "meta": {
                "rank_math_focus_keyword": content["focus_kw"],
                "rank_math_description":  content["meta_desc"],
            },
        }
        if media_id:
            payload["featured_media"] = media_id
        if cat_id:
            payload["categories"] = [cat_id]
        r = requests.post(f"{WP_SITE_URL}/wp-json/wp/v2/posts",
                          headers=_wp_auth(), json=payload, timeout=30)
        r.raise_for_status()
        result = r.json()
        post_id = result.get("id")
        span.set_attribute("post_id",  str(post_id or ""))
        span.set_attribute("post_url", result.get("link", ""))
        logger.info(f"WordPress 발행 완료: {result.get('link')}")

        # 포커스키워드·메타설명 PATCH (RankMath REST 필드 미등록 시 발행 후 재시도)
        if post_id:
            try:
                requests.post(
                    f"{WP_SITE_URL}/wp-json/wp/v2/posts/{post_id}",
                    headers=_wp_auth(),
                    json={"meta": {
                        "rank_math_focus_keyword": content["focus_kw"],
                        "rank_math_description":  content["meta_desc"],
                    }},
                    timeout=15,
                )
            except Exception as e:
                logger.warning(f"포커스키워드 PATCH 실패: {e}")

        return result


# ==========================================
# 13. Pinterest API v5
# ==========================================

def pinterest_create_pin(img_bytes: bytes, title: str, description: str, post_url: str) -> Optional[str]:
    if not PINTEREST_TOKEN or not PINTEREST_BOARD_ID:
        logger.warning("Pinterest 미설정 — 건너뜁니다.")
        return None
    with tracer.start_as_current_span("pinterest_create_pin") as span:
        auth = {"Authorization": f"Bearer {PINTEREST_TOKEN}", "Content-Type": "application/json"}
        try:
            payload = {
                "board_id":    PINTEREST_BOARD_ID,
                "title":       title[:100],
                "description": description[:500],
                "link":        post_url,
                "media_source": {
                    "source_type":  "image_base64",
                    "content_type": "image/jpeg",
                    "data":         base64.b64encode(img_bytes).decode(),
                },
            }
            r = requests.post("https://api.pinterest.com/v5/pins", headers=auth, json=payload, timeout=60)
            if r.status_code == 201:
                pin_id = r.json().get("id", "")
                span.set_attribute("pin_id", pin_id)
                logger.info(f"Pinterest 핀 생성 완료: {pin_id}")
                return pin_id
            logger.warning(f"Pinterest 업로드 실패: {r.status_code} {r.text[:200]}")
        except Exception as e:
            logger.error(f"Pinterest 오류: {e}")
        return None


# ==========================================
# 14. 텔레그램
# ==========================================

def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning(f"텔레그램 실패: {r.text[:100]}")
    except Exception as e:
        logger.warning(f"텔레그램 오류: {e}")


# ==========================================
# 15. 메인 실행
# ==========================================

def run():
    with tracer.start_as_current_span("run") as root:
        t0 = datetime.now(timezone.utc)
        logger.info("=== trip.bestwellth.org 자동화 시작 ===")
        send_telegram("trip.bestwellth.org 여행 블로그 자동화 시작")

        # Step 1: 이미 발행된 여행지 수집 (중복 방지용)
        published_set = wp_get_published_destinations()

        # Step 1.5: 대륙 로테이션 + Gemini로 오늘의 경이로운 여행지 발굴
        continent = get_today_continent()
        destinations = fetch_trending_destinations(published=published_set)

        # 혹시 발행된 게 섞여 있으면 한 번 더 필터
        destinations_filtered = [d for d in destinations if not is_already_published(d, published_set)]
        if destinations_filtered:
            destinations = destinations_filtered
        else:
            logger.warning("모든 후보 여행지가 이미 발행됨 — 중복 허용하고 전체 목록으로 진행")

        send_telegram(
            f"오늘의 대륙: {continent}\n\n여행지 후보:\n"
            + "\n".join(f"{i}. {d}" for i, d in enumerate(destinations, 1))
        )

        # Step 1.6: "Famous | Hidden" 쌍 파싱
        destination_pairs = []
        for d in destinations:
            if '|' in d:
                parts = [p.strip() for p in d.split('|', 1)]
                if len(parts) == 2:
                    destination_pairs.append((parts[0], parts[1]))
        if not destination_pairs:
            destination_pairs = [(d, d) for d in destinations]

        # Step 2: 가이드북 스타일
        style_guide = fetch_guidebook_style(destination_pairs[0][1])

        # Step 3: 데이터 수집 — 유명 + 연계 여행지 모두
        travel_data_famous, travel_data_hidden, famous, selected = None, None, None, None
        for famous_cand, hidden_cand in destination_pairs:
            try:
                td_hidden = fetch_travel_data(hidden_cand)
                # 유명 여행지 데이터 — 실패해도 최소 dict으로 대체
                try:
                    td_famous = fetch_travel_data(famous_cand)
                except Exception:
                    td_famous = {"destination": famous_cand, "overview": "", "attractions": "",
                                 "food": "", "transport": "", "accommodation": "", "tips": "", "sources": []}
                travel_data_famous = td_famous
                travel_data_hidden = td_hidden
                famous   = famous_cand
                selected = hidden_cand
                break
            except ValueError as e:
                logger.warning(f"'{hidden_cand}' 건너뜀: {e}")
            except Exception as e:
                logger.error(f"'{hidden_cand}' 예외: {e}")

        # Special Destinations 전체 실패 시 — 6대륙 중 랜덤으로 재시도
        if not travel_data_hidden and continent == "Special Destinations":
            import random
            fallback_continents = [c for c in _ROTATION_ORDER if c != "Special Destinations"]
            random.shuffle(fallback_continents)
            logger.warning("Special Destinations 전체 실패 — 6대륙 랜덤 폴백 시작")
            send_telegram("Special Destinations 데이터 부족 — 6대륙 랜덤으로 재시도")
            for fb_continent in fallback_continents:
                fb_destinations = fetch_trending_destinations(published=published_set)
                fb_pairs = []
                for d in fb_destinations:
                    if '|' in d:
                        parts = [p.strip() for p in d.split('|', 1)]
                        if len(parts) == 2:
                            fb_pairs.append((parts[0], parts[1]))
                if not fb_pairs:
                    fb_pairs = [(d, d) for d in fb_destinations]
                for famous_cand, hidden_cand in fb_pairs:
                    try:
                        td_hidden = fetch_travel_data(hidden_cand)
                        try:
                            td_famous = fetch_travel_data(famous_cand)
                        except Exception:
                            td_famous = {"destination": famous_cand, "overview": "", "attractions": "",
                                         "food": "", "transport": "", "accommodation": "", "tips": "", "sources": []}
                        travel_data_famous = td_famous
                        travel_data_hidden = td_hidden
                        famous   = famous_cand
                        selected = hidden_cand
                        continent = fb_continent
                        logger.info(f"폴백 성공: {fb_continent} — {famous_cand} | {hidden_cand}")
                        break
                    except ValueError as e:
                        logger.warning(f"폴백 '{hidden_cand}' 건너뜀: {e}")
                    except Exception as e:
                        logger.error(f"폴백 '{hidden_cand}' 예외: {e}")
                if travel_data_hidden:
                    break

        if not travel_data_hidden:
            msg = "모든 후보 여행지 데이터 수집 실패"
            logger.error(msg)
            send_telegram(f"자동화 실패: {msg}")
            return

        root.set_attribute("destination", selected)
        root.set_attribute("famous", famous)
        root.set_attribute("continent", continent)

        # Step 4: Gemini 콘텐츠
        try:
            content = generate_content(travel_data_famous, travel_data_hidden, style_guide, continent)
        except Exception as e:
            send_telegram(f"자동화 실패: 콘텐츠 생성 {e}")
            return

        logger.info(f"제목: {content['title']}")

        # Step 4.5: 예약 버튼 처리 (URL 유효성 검사 → 유효하면 버튼, 아니면 안내 문구)
        ticket_btns = build_action_buttons(
            validate_urls(content.get("ticket_urls", [])),
            label_suffix="티켓 예매", bg_color="#0369a1",
            fallback_text=f"공식 사이트 또는 GetYourGuide·Klook에서 {selected} 입장권을 예매할 수 있습니다.",
        )
        transport_btns = build_action_buttons(
            validate_urls(content.get("transport_urls", [])),
            label_suffix="예매하기", bg_color="#0c4a6e",
            fallback_text=f"{selected} 교통편은 현지 공항 또는 공식 운송사 사이트에서 예매할 수 있습니다.",
        )
        # 맞춤형 숙소 CTA (도시명 포함 문구)
        hotel_btns = build_hotel_buttons_custom(selected)

        # 투어·액티비티 버튼
        top_tour = _get_top_tour(selected, content.get("overview", ""))
        logger.info(f"필수 투어: {top_tour}")
        tour_btns = build_tour_buttons(selected, top_tour)

        content["body"] = content["body"].replace("{TICKET_BUTTONS}", ticket_btns)
        content["body"] = content["body"].replace("{TRANSPORT_BUTTONS}", transport_btns)
        content["body"] = content["body"].replace("{HOTEL_BUTTONS}", hotel_btns)
        content["body"] = content["body"].replace("{TOUR_BUTTONS}", tour_btns)

        # Step 4.6: 크롤링 기반 등급 테이블 생성
        try:
            attraction_ticket_table = build_attraction_ticket_table(
                content.get("attractions", []), selected, CAT_COLOR
            )
        except Exception as e:
            logger.warning(f"관광지 티켓 테이블 생성 실패: {e}")
            attraction_ticket_table = ""
        try:
            accommodation_table = build_accommodation_table(
                content.get("hotels", []), selected, CAT_COLOR
            )
        except Exception as e:
            logger.warning(f"숙소 테이블 생성 실패: {e}")
            accommodation_table = ""
        try:
            transport_classes_table = build_transport_classes_table(
                content.get("transport_services", []), selected, CAT_COLOR
            )
        except Exception as e:
            logger.warning(f"교통 등급 테이블 생성 실패: {e}")
            transport_classes_table = ""
        content["body"] = content["body"].replace("{ATTRACTION_TICKET_TABLE}", attraction_ticket_table)
        content["body"] = content["body"].replace("{ACCOMMODATION_TABLE}", accommodation_table)
        content["body"] = content["body"].replace("{TRANSPORT_CLASSES_TABLE}", transport_classes_table)

        # Step 4.9: 공식 관광 사이트 이미지 우선 수집
        official_img_pool: List[bytes] = []
        _official_hashes: set = set()
        try:
            off_urls = _get_official_tourism_urls(selected)
            logger.info(f"공식 관광 사이트: {off_urls}")
            for off_url in off_urls:
                imgs = _crawl_official_site_images(off_url, _official_hashes)
                official_img_pool.extend(imgs)
                if len(official_img_pool) >= 10:
                    break
            logger.info(f"공식 사이트 이미지 {len(official_img_pool)}장 수집 완료")
        except Exception as e:
            logger.warning(f"공식 사이트 수집 실패: {e}")

        # Step 5: 실사 이미지 수집 (공식 사이트 풀 우선, 이후 API fallback)
        used_urls: set = set()

        def _pick_from_pool_or_api(section: str, orientation: str = "landscape", query: str = "") -> Optional[bytes]:
            """공식 사이트 풀에서 먼저 꺼내고 없으면 API로 fallback."""
            # featured·attraction·tips 섹션은 공식 풀 우선 사용
            if official_img_pool and section in ("featured", "attraction", "tips"):
                img = official_img_pool.pop(0)
                logger.info(f"[공식사이트풀] {section} 이미지 사용")
                return img
            return fetch_travel_image(selected, orientation=orientation, section=section,
                                      query=query, used_urls=used_urls)

        img_landscape = _pick_from_pool_or_api("featured", orientation="landscape")
        img_portrait  = fetch_travel_image(selected, orientation="portrait", section="portrait", used_urls=used_urls)

        # Pinterest용 2:3 세로 이미지 2장 수집
        pin_img1 = fetch_travel_image(selected, orientation="portrait", section="attraction", used_urls=used_urls)
        pin_img2 = fetch_travel_image(selected, orientation="portrait", section="tips",       used_urls=used_urls)

        img_pin = None
        if img_portrait:
            try:
                img_pin = crop_to_ratio(img_portrait, width=1000, height=1500)
            except Exception as e:
                logger.warning(f"이미지 리사이즈 실패: {e}")
                img_pin = img_portrait

        img_wp = None
        if img_landscape:
            try:
                img_wp = crop_to_ratio(img_landscape, width=1200, height=675)
            except Exception as e:
                logger.warning(f"이미지 리사이즈 실패: {e}")
                img_wp = img_landscape

        # Step 6: WP 이미지 업로드
        today = datetime.now().strftime("%Y%m%d")
        media_id = None
        if img_wp:
            fname = f"{selected.lower().replace(' ', '_')}_{today}.jpg"
            media_result = wp_upload_image(img_wp, fname, alt=f"{selected} 여행")
            if media_result:
                media_id = media_result.get("id")
                img_url = media_result.get("url", "")
                if img_url:
                    photo_html = (
                        f'<figure style="margin:32px 0;text-align:center;">'
                        f'<img src="{img_url}" alt="{selected} 여행" '
                        f'style="width:100%;max-width:900px;height:auto;border-radius:12px;object-fit:cover;" />'
                        f'<figcaption style="margin-top:8px;font-size:12px;color:#94a3b8;">'
                        f'{selected} &middot; Photo via Unsplash/Pexels</figcaption>'
                        f'</figure>'
                    )
                    insert_pos = content["body"].find("</div>")
                    if insert_pos != -1:
                        insert_pos += 6
                        content["body"] = content["body"][:insert_pos] + photo_html + content["body"][insert_pos:]

        # Step 6.3: Pinterest 2:3 세로 이미지 2장 업로드 → {PINTEREST_IMAGES} 교체
        pin_html = ""
        pin_uploaded = []
        for idx, pin_raw in enumerate([pin_img1, pin_img2], start=1):
            if not pin_raw:
                continue
            try:
                pin_cropped = crop_to_ratio(pin_raw, width=600, height=900)
            except Exception:
                pin_cropped = pin_raw
            pin_fname = f"{selected.lower().replace(' ', '_')}_pin{idx}_{today}.jpg"
            pin_media = wp_upload_image(pin_cropped, pin_fname, alt=f"{selected} 여행 {idx}")
            if pin_media and pin_media.get("url"):
                pin_uploaded.append(pin_media["url"])

        if len(pin_uploaded) >= 2:
            pin_html = (
                f'<div style="display:flex;gap:12px;margin:24px 0;">'
                f'<figure style="flex:1;margin:0;">'
                f'<img src="{pin_uploaded[0]}" alt="{selected} 명소" '
                f'style="aspect-ratio:2/3;object-fit:cover;width:100%;border-radius:12px;" />'
                f'<figcaption style="margin-top:6px;font-size:11px;color:#94a3b8;text-align:center;">'
                f'{selected} · 명소</figcaption></figure>'
                f'<figure style="flex:1;margin:0;">'
                f'<img src="{pin_uploaded[1]}" alt="{selected} 풍경" '
                f'style="aspect-ratio:2/3;object-fit:cover;width:100%;border-radius:12px;" />'
                f'<figcaption style="margin-top:6px;font-size:11px;color:#94a3b8;text-align:center;">'
                f'{selected} · 풍경</figcaption></figure>'
                f'</div>'
            )
        elif len(pin_uploaded) == 1:
            pin_html = (
                f'<figure style="margin:24px 0;">'
                f'<img src="{pin_uploaded[0]}" alt="{selected} 명소" '
                f'style="aspect-ratio:2/3;object-fit:cover;width:50%;border-radius:12px;" />'
                f'</figure>'
            )
        content["body"] = content["body"].replace("{PINTEREST_IMAGES}", pin_html)

        # Step 6.5: 섹션별 이미지 수집·업로드 후 플레이스홀더 교체
        # PHOTO:famous는 유명 여행지 이름으로 별도 검색
        if "{PHOTO:famous}" in content["body"]:
            try:
                famous_img = fetch_travel_image(famous, orientation="landscape", section="attraction", used_urls=used_urls)
                if famous_img:
                    try:
                        famous_img = crop_to_ratio(famous_img, width=900, height=500)
                    except Exception:
                        pass
                    famous_fname = f"{famous.lower().replace(' ', '_')}_famous_{today}.jpg"
                    famous_media = wp_upload_image(famous_img, famous_fname, alt=f"{famous} 여행")
                    if famous_media and famous_media.get("url"):
                        famous_html = (
                            f'<figure style="margin:20px 0 24px;">'
                            f'<img src="{famous_media["url"]}" alt="{famous} 여행" '
                            f'style="width:100%;max-width:900px;height:auto;border-radius:12px;object-fit:cover;" />'
                            f'<figcaption style="margin-top:6px;font-size:12px;color:#94a3b8;">'
                            f'{famous} &middot; Photo via Unsplash/Pexels</figcaption>'
                            f'</figure>'
                        )
                        content["body"] = content["body"].replace("{PHOTO:famous}", famous_html)
            except Exception as e:
                logger.warning(f"유명 여행지 이미지 실패: {e}")
            content["body"] = content["body"].replace("{PHOTO:famous}", "")

        for section_key in ("attraction", "attraction2", "attraction3", "food", "transport", "tips"):
            placeholder = f'{{PHOTO:{section_key}}}'
            if placeholder not in content["body"]:
                continue
            try:
                # 교통 섹션은 여행지 실제 교통수단으로 Gemini가 맞춤 쿼리 생성
                # 음식 섹션은 목적지 고유 음식 API 검색
                # attraction·tips 섹션은 공식 사이트 풀 우선 사용
                api_section = "attraction" if section_key in ("attraction2", "attraction3") else section_key
                if section_key == "transport":
                    transport_queries = _get_transport_queries(
                        selected,
                        content.get("transport_services", []),
                        content.get("transport", ""),
                    )
                    sec_img = None
                    for tq in transport_queries:
                        sec_img = fetch_travel_image(selected, orientation="landscape", query=tq, section="transport", used_urls=used_urls)
                        if sec_img:
                            logger.info(f"[교통 이미지] 쿼리 성공: '{tq}'")
                            break
                else:
                    sec_img = _pick_from_pool_or_api(api_section, orientation="landscape")
                if sec_img:
                    try:
                        sec_img = crop_to_ratio(sec_img, width=900, height=500)
                    except Exception:
                        pass
                    sec_fname = f"{selected.lower().replace(' ', '_')}_{section_key}_{today}.jpg"
                    sec_media = wp_upload_image(sec_img, sec_fname, alt=f"{selected} {section_key}")
                    if sec_media and sec_media.get("url"):
                        sec_url = sec_media["url"]
                        sec_html = (
                            f'<figure style="margin:20px 0 24px;">'
                            f'<img src="{sec_url}" alt="{selected} {section_key}" '
                            f'style="width:100%;max-width:900px;height:auto;border-radius:12px;object-fit:cover;" />'
                            f'<figcaption style="margin-top:6px;font-size:12px;color:#94a3b8;">'
                            f'{selected} &middot; Photo via Unsplash/Pexels</figcaption>'
                            f'</figure>'
                        )
                        content["body"] = content["body"].replace(placeholder, sec_html)
                        continue
            except Exception as e:
                logger.warning(f"섹션 이미지 실패 ({section_key}): {e}")
            content["body"] = content["body"].replace(placeholder, "")

        # Step 7: 카테고리 (지역별 자동 분류)
        region = classify_region(selected)
        logger.info(f"지역 카테고리: {selected} → {region}")
        cat_id = wp_get_or_create_category(region)

        # Step 8: WordPress 발행
        try:
            wp_result = wp_publish(content, media_id, cat_id)
            post_url = wp_result.get("link", "")
        except Exception as e:
            send_telegram(f"자동화 실패: WordPress 발행 {e}")
            return

        # Step 9: Pinterest
        pin_id = None
        if img_pin and post_url:
            pin_desc = content["excerpt"] or f"{selected} 여행 완전 정복. 명소·맛집·교통 총정리."
            pin_id = pinterest_create_pin(img_pin, content["title"], pin_desc, post_url)

        elapsed = int((datetime.now(timezone.utc) - t0).total_seconds())
        summary = (
            f"<b>trip.bestwellth.org 자동 발행 완료</b>\n\n"
            f"대륙: {continent}\n"
            f"여행지: {famous} → {selected}\n"
            f"제목: {content['title']}\n"
            f"URL: {post_url}\n"
            f"Pinterest: {pin_id or '미연동'}\n"
            f"소요: {elapsed}초"
        )
        logger.info(summary.replace("<b>", "").replace("</b>", ""))
        send_telegram(summary)
        root.set_attribute("post_url", post_url)
        root.set_attribute("elapsed_seconds", elapsed)


if __name__ == "__main__":
    run()
