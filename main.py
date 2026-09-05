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
from typing import Optional, List, Dict, Tuple
from urllib.parse import quote

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import socket
import requests
import urllib3.util.connection as urllib3_conn
from bs4 import BeautifulSoup
from PIL import Image
import google.generativeai as genai

# PythonAnywhere는 아웃바운드 IPv6를 지원하지 않아 IPv6 DNS 응답이 오면
# "Network is unreachable"로 실패한다. requests/urllib3가 항상 IPv4로만
# 연결하도록 강제해 이 문제를 우회한다.
def _allowed_gai_family():
    return socket.AF_INET

urllib3_conn.allowed_gai_family = _allowed_gai_family

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
GOOGLE_MAPS_KEY     = os.getenv("GOOGLE_MAPS_KEY", "")
PIXABAY_KEY         = os.getenv("PIXABAY_API_KEY", "")
SERPAPI_KEY         = os.getenv("SERPAPI_KEY", "")
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
            f"Select 6 destination PAIRS from {continent}. Each pair = Gateway City | Specific Spot.\n\n"
            f"Rules:\n"
            f"- Gateway City: the MOST-SEARCHED, highest-traffic major city Koreans actually look up "
            f"  (e.g. Tokyo, Paris, Bangkok, Rome, London, Kyoto, Osaka, Prague) — do NOT avoid major capitals, "
            f"  these high-search-volume cities are the priority. The same Gateway City MAY repeat across different pairs "
            f"  (e.g. Tokyo can appear multiple times paired with different specific spots) since it is only used as travel context.\n"
            f"- Specific Spot: NOT a whole city, region, or broad area — must be ONE narrow, concrete place: "
            f"  a single neighborhood/district, a specific attraction, a small town, or one national park/temple/market, "
            f"  reachable from the Gateway City within ~3 hours "
            f"  (e.g. Tokyo | Nikko, Tokyo | Kamakura, Tokyo | Odaiba, Tokyo | Yanaka Ginza, Bangkok | Amphawa Floating Market)\n"
            f"- This Specific Spot is the actual subject of the article — it must be narrow enough that a full article "
            f"  can go deep on it without feeling stretched thin (avoid selecting an entire city or province as the Specific Spot)\n"
            f"- Avoid already-published Specific Spots above (the Gateway City itself repeating is fine, only the Specific Spot must be new)\n"
            f"- Both places must be in the same country or very nearby region\n\n"
            f"Reply with exactly 6 pairs — one per line in format 'Gateway City | Specific Spot', nothing else."
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
# 7.5 실용정보 체크리스트 주제 발굴 (trip.com식 총정리형)
# ==========================================

_PRACTICAL_TOPIC_TYPES = [
    "여행 준비물 총정리",
    "유심·이심·로밍 완벽정리",
    "여행자보험 가입 가이드",
    "항공권 & 호텔 예약 총정리",
    "입장권 & 근처 명소 총정리",
    "현지 교통 & 패스 완벽정리",
    "환전 & 카드 사용법 총정리",
]

_PRACTICAL_DEST_POOL: Dict[str, List[str]] = {
    "Asia": ["일본", "태국", "베트남", "대만", "필리핀", "싱가포르", "말레이시아", "발리"],
    "Europe": ["프랑스", "이탈리아", "스페인", "영국", "스위스", "체코", "그리스", "포르투갈"],
    "North America": ["미국", "캐나다", "멕시코"],
    "South America": ["페루", "브라질", "아르헨티나"],
    "Africa": ["모로코", "이집트", "남아프리카공화국"],
    "Oceania": ["호주", "뉴질랜드", "괌", "사이판"],
    "Special Destinations": ["몰디브", "두바이", "터키", "아이슬란드"],
}


def is_topic_already_published(destination: str, topic: str, published: set) -> bool:
    """목적지+주제 조합이 이미 다뤄졌는지 대략 확인 (제목/슬러그 substring 매칭)."""
    dest_lower = destination.lower()
    topic_key = topic.split(" ")[0].lower()
    return any(dest_lower in item and topic_key in item for item in published)


def fetch_practical_topics(published: Optional[set] = None) -> List[Dict[str, str]]:
    """실용정보/체크리스트형 콘텐츠를 위한 '목적지 + 주제' 조합을 발굴합니다.
    트립닷컴 블로그식 총정리형 포맷 — 스토리텔링이 아니라 여행 준비 과정에서
    실제로 검색하는 실무 정보(준비물·유심·보험·항공권 등)를 다룹니다.
    """
    with tracer.start_as_current_span("fetch_practical_topics") as span:
        continent = get_today_continent()
        span.set_attribute("continent", continent)
        pool = _PRACTICAL_DEST_POOL.get(continent, [])
        published_list = ", ".join(list(published)[:30]) if published else "없음"

        prompt = (
            f"당신은 한국 여행객을 대상으로 하는 여행 콘텐츠 전략가입니다.\n\n"
            f"오늘의 대륙: {continent}\n"
            f"후보 목적지 풀: {', '.join(pool)}\n"
            f"이미 다룬 '목적지+주제' 조합 (반드시 피할 것): {published_list}\n\n"
            f"6개의 '목적지 | 실용주제' 조합을 선정하세요.\n\n"
            f"규칙:\n"
            f"- 목적지: 한국인이 실제로 많이 검색하는 나라 또는 대표 도시 (예: 일본, 태국, 발리, 파리)\n"
            f"- 실용주제: 여행 '준비 과정'에서 실제로 검색하는 실무 정보 하나. "
            f"예시: 여행 준비물, 유심·이심, 여행자보험, 항공권&호텔 예약, 입장권&근처 명소, 현지교통&패스, 환전&카드\n"
            f"- 스토리텔링/관광 소개가 아니라 '검색하면 바로 답을 얻고 싶은' 실용 정보여야 함\n"
            f"- 이미 다룬 조합은 피하고, 같은 목적지라도 다른 주제면 사용 가능\n\n"
            f"6줄, 한 줄에 하나씩 '목적지 | 실용주제' 형식으로만 답하세요."
        )

        try:
            resp = gemini.generate_content(prompt)
            topics = []
            for line in resp.text.strip().splitlines():
                line = re.sub(r'^[\d\.\-\)\s]+', '', line).strip()
                line = re.sub(r'["""\'*]', '', line).strip()
                if '|' in line:
                    parts = [p.strip() for p in line.split('|', 1)]
                    if len(parts) == 2 and all(parts):
                        topics.append({"destination": parts[0], "topic": parts[1]})
            if topics:
                logger.info(f"Gemini 발굴 실용주제 ({continent}): {topics}")
                span.set_attribute("source", f"gemini+{continent}")
                return topics[:6]
        except Exception as e:
            logger.warning(f"Gemini 실용주제 발굴 실패: {e}")

        import random
        fallback_dest = pool.copy() or ["일본", "태국", "베트남"]
        random.shuffle(fallback_dest)
        fallback_topics = _PRACTICAL_TOPIC_TYPES.copy()
        random.shuffle(fallback_topics)
        span.set_attribute("source", "pool_fallback")
        logger.warning(f"Gemini 실패 — pool fallback ({continent})")
        return [
            {"destination": d, "topic": t}
            for d, t in zip(fallback_dest, fallback_topics)
        ][:6]


def build_checklist_prompt(destination: str, topic: str, continent: str = "") -> str:
    year = datetime.now().year
    coupang_disclosure = "" if not COUPANG_LINK else (
        '<p style="margin-top:24px;font-size:12px;color:#94a3b8;text-align:center;line-height:1.8;">'
        '이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.</p>'
    )

    return f"""
당신은 trip.bestwellth.org의 여행 실용정보 에디터입니다.
아래 [목적지]와 [주제]를 바탕으로 '체크리스트/총정리형' 블로그 포스팅 HTML을 작성하세요.
참고 스타일: 트립닷컴 블로그처럼 목차(TOC) + 소제목별 이미지 1장 + 캐주얼한 말투 + 핵심 키워드 형광펜 강조.

[목적지] {destination}
[주제] {topic}
[오늘의 대륙] {continent or '전 세계'}

[문체]
- 친근하고 캐주얼한 존댓말 ("~해요", "~하죠", "~해주세요")
- 이모지·아이콘·이모티콘 전면 사용 금지 (제목·본문 어디에도 넣지 않음)
- 정보는 정확하고 실용적으로 — 지어낸 수치·금액 절대 금지
- Markdown 기호(**, ##, -, *) 본문 삽입 금지

[절대 금지 사항]
- 개인 일기·경험 형식 금지 ("저는 다녀왔습니다" 등 — 정보 제공자 시점 유지, "~해요" 톤은 허용)
- 본문에 외부 링크(href 포함 a태그) 직접 삽입 금지
- 불확실한 가격·정책을 단정적으로 서술 금지 — "항공사/현지 정책에 따라 다를 수 있다"는 식으로 안내
- [SECTIONS] 태그의 소제목·개수는 본문 h2와 정확히 1:1 대응해야 함 (누락·추가 금지)
- {{HOTEL_BUTTONS}}, {{TOUR_BUTTONS}}, {{COUPANG_BLOCK}}, {{PHOTO:section_N}} 같은 중괄호 두 겹({{ }}) 플레이스홀더는 절대 다른 텍스트로 바꾸거나 삭제하지 말고 그대로 출력할 것 (실제 예약 버튼·사진으로 자동 치환됨)

[버튼 배치 지침 — 맥락에 맞게 분산 배치, 한곳에 몰아넣지 말 것]
{{HOTEL_BUTTONS}}, {{TOUR_BUTTONS}}, {{COUPANG_BLOCK}} 3개는 아래 [SECTIONS] 본문 섹션들 중 각각 가장 문맥이 어울리는 섹션 하나를 골라 그 섹션의 <p> 문단 바로 뒤에 자연스럽게 삽입하세요. 절대 마지막에 한꺼번에 모아서 넣지 마세요.
- {{HOTEL_BUTTONS}}: 숙소·호텔·체크인·숙박비 관련 내용을 다루는 섹션에 삽입. 그런 섹션이 없으면 가장 관련 있는 섹션 하나를 골라 삽입.
- {{TOUR_BUTTONS}}: 입장권·투어·액티비티·현지 프로그램 예약 관련 내용을 다루는 섹션에 삽입.
- {{COUPANG_BLOCK}}: 짐·캐리어·보조배터리 등 여행 준비물 관련 내용을 다루는 섹션에 삽입. 그런 섹션이 없으면 가장 관련 있는 섹션 하나를 골라 삽입.
3개는 서로 다른 섹션에 분산시키고 (같은 섹션에 2개 이상 몰아넣지 말 것), 본문 전체에서 각각 정확히 1번씩만 등장해야 합니다.

[HTML 구조 — 반드시 이 순서로]

카테고리 색상: {CAT_COLOR} | 라이트 배경: {CAT_LIGHT_BG} | 라이트 테두리: {CAT_LIGHT_BORDER}

--- 1. 카테고리 뱃지 ---
<div style="display:inline-block;background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:13px;font-weight:700;padding:4px 14px;border-radius:20px;margin-bottom:14px;">여행 준비 가이드 · {destination}</div>

--- 2. 인트로 (친근한 톤, 이모지·아이콘 없이) ---
<p style="font-size:16px;color:#334155;line-height:1.9;margin-bottom:8px;">[{destination} 여행을 준비한다면 미리 챙겨야 할 것들을 정리해드릴게요. 이 주제가 왜 중요한지 2~3문장으로 자연스럽게 설명]</p>

--- 3. 목차(TOC) 박스 ---
<div style="background:#f8fafc;padding:24px 28px;border-radius:16px;border:1px solid #e2e8f0;margin:24px 0 40px 0;">
  <p style="margin:0 0 14px 0;font-size:14px;font-weight:800;color:#0f172a;">목차</p>
  <ul style="list-style:none;padding:0;margin:0;">
    [소제목 li 태그 5~7개 — 아래 [SECTIONS]와 정확히 동일한 순서·문구로 작성. 형식: <li style="font-size:14px;color:#334155;line-height:2.0;">— [소제목]</li>]
  </ul>
</div>

[[[AD_DISPLAY]]]

--- 4. 본문 섹션 (5~7개, [SECTIONS]와 1:1 정확히 대응) ---
아래 블록을 섹션 개수만큼 반복하되 {{PHOTO:section_N}}의 N은 1부터 순서대로 증가시킬 것:
<div style="margin-bottom:44px;">
  <h2 style="font-size:clamp(18px,3vw,21px);font-weight:800;color:#0f172a;margin:0 0 16px 0;">[소제목]</h2>
  {{PHOTO:section_N}}
  <p style="font-size:15px;color:#334155;line-height:1.9;margin-bottom:12px;">[본문 — 핵심 키워드는 <span style="background-color:{CAT_LIGHT_BG};padding:2px 6px;color:{CAT_COLOR};font-weight:700;">이렇게</span> 형광펜 강조. 3~5문장, 실용적 정보 위주]</p>
  [이 섹션이 위 [버튼 배치 지침]에서 골라야 할 섹션에 해당한다면 여기, 마지막 문단 바로 뒤에 해당 플레이스홀더({{HOTEL_BUTTONS}} / {{TOUR_BUTTONS}} / {{COUPANG_BLOCK}} 중 하나)를 삽입. 해당 없으면 생략.]
</div>
(섹션 5~7개 중 정확히 3개 섹션에만 위 방식으로 플레이스홀더가 하나씩 들어가고, 나머지 섹션에는 들어가지 않음)

전체 섹션 중 중간 지점(3~4번째 섹션 뒤)에 [[[AD_IN_ARTICLE]]]를 한 번 삽입할 것.

--- 5. 체크리스트 요약 박스 ---
<div style="background:{CAT_LIGHT_BG};border-left:4px solid {CAT_COLOR};padding:20px 24px;border-radius:0 12px 12px 0;margin:32px 0;">
  <p style="margin:0 0 10px 0;font-size:13px;font-weight:800;color:{CAT_COLOR};">한눈에 보는 체크리스트</p>
  <ul style="margin:0;padding-left:18px;font-size:14px;color:#334155;line-height:2.0;">[섹션별 핵심 1줄씩 li 태그로 요약 — 섹션 개수만큼]</ul>
</div>

--- 6. 면책 조항 ---
<div style="margin-top:2em;padding:20px 24px;background:#fafafa;border-radius:12px;border:1px solid #e2e8f0;">
  <p style="margin:0 0 8px 0;font-size:13px;font-weight:700;color:#64748b;">안내</p>
  <p style="margin:0;font-size:13px;color:#94a3b8;line-height:1.8;">본 콘텐츠는 정보 제공을 목적으로 작성되었으며, 실제 정책·요금·조건은 항공사·통신사·현지 기관 사정에 따라 달라질 수 있습니다. 예약·구매 전 공식 채널에서 최신 정보를 확인해 주세요.</p>
</div>

[[[AD_AUTORELAXED]]]

{coupang_disclosure}

[응답 형식 — 맨 끝에 순서대로 출력]
[TITLE]
- 형식: 【{year} {destination} 여행】 {topic} 관련 핵심주제 & 서브키워드 총정리! 형태 (트립닷컴 스타일)
- 대괄호(【 】)로 연도+목적지를 감싸고, "총정리"/"완벽정리" 등으로 마무리
- 40자 이내, 이모지 사용 금지
[/TITLE]
[COUNTRY_KR]{destination}이 속한 국가명을 한국어로 (도시명이면 그 도시가 속한 국가, 최대 6자)[/COUNTRY_KR]
[FOCUS_KW]3~4단어 한국어 롱테일 키워드 (예: 일본 여행 준비물)[/FOCUS_KW]
[META_DESC]130~155자 메타 설명[/META_DESC]
[SLUG]{destination}과 주제를 반영한 3~6단어 영문 하이픈 슬러그[/SLUG]
[EXCERPT]100~150자 발췌문[/EXCERPT]
[SECTIONS]
소제목|이미지검색영문키워드|섹션 한줄요약(체크리스트용)
(위 형식으로 5~7줄, 본문 섹션 h2와 정확히 같은 순서·개수. 이미지검색영문키워드는 스톡사진 검색에 바로 쓸 수 있는 구체적 영문 키워드로 작성, 예: "japan passport visa document")
[/SECTIONS]
"""


def _parse_checklist(raw: str, destination: str, topic: str) -> Dict:
    def ex(tag: str, default: str = "") -> str:
        m = re.search(rf'\[{tag}\](.*?)\[/{tag}\]', raw, re.DOTALL)
        return m.group(1).strip() if m else default

    body = raw
    for tag in ["TITLE", "FOCUS_KW", "META_DESC", "SLUG", "EXCERPT", "COUNTRY_KR", "SECTIONS"]:
        body = re.sub(rf'\[{tag}\].*?\[/{tag}\]\n?', '', body, flags=re.DOTALL)
    body = re.sub(r'^```(?:html)?\s*\n?', '', body.strip(), flags=re.IGNORECASE)
    body = re.sub(r'\n?```\s*$', '', body, flags=re.IGNORECASE)
    body = body.strip()

    body = re.sub(r'<a(?![^>]*\brel=)[^>]*>(.*?)</a>', r'\1', body, flags=re.DOTALL)
    # wpautop이 광고 스크립트를 깨뜨리지 않도록 wp:html 블록으로 감싼다
    body = body.replace('[[[AD_DISPLAY]]]', f'<!-- wp:html -->{AD_DISPLAY}<!-- /wp:html -->')
    body = body.replace('[[[AD_IN_ARTICLE]]]', f'<!-- wp:html -->{AD_IN_ARTICLE}<!-- /wp:html -->')
    body = body.replace('[[[AD_AUTORELAXED]]]', f'<!-- wp:html -->{AD_AUTORELAXED}<!-- /wp:html -->')

    sections = []
    for line in ex("SECTIONS").splitlines():
        line = line.strip()
        if not line or '|' not in line:
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 2 and parts[0] and parts[1]:
            sections.append({
                "heading": parts[0],
                "query":   parts[1],
                "summary": parts[2] if len(parts) > 2 else parts[0],
            })

    raw_title  = ex("TITLE", f"{destination} {topic} 총정리")
    country_kr = ex("COUNTRY_KR", "").strip()
    full_title = f"[{country_kr}] {raw_title}" if country_kr else raw_title
    slug_base  = f"{destination}-{topic}".lower().replace(' ', '-').replace('·', '-')
    slug_base  = re.sub(r'[^a-z0-9\-]', '', slug_base) or "travel-guide"

    return {
        "destination": destination,
        "topic":       topic,
        "title":       full_title,
        "country_kr":  country_kr,
        "focus_kw":    ex("FOCUS_KW",  f"{destination} {topic}"),
        "meta_desc":   ex("META_DESC", f"{destination} {topic} 총정리. 꼭 필요한 정보만 정리했습니다."),
        "slug":        ex("SLUG", slug_base[:80]),
        "excerpt":     ex("EXCERPT", ""),
        "sections":    sections,
        "body":        body,
    }


def generate_checklist_content(destination: str, topic: str, continent: str = "") -> Dict:
    with tracer.start_as_current_span("generate_checklist_content") as span:
        span.set_attribute("destination", destination)
        span.set_attribute("topic", topic)
        prompt = build_checklist_prompt(destination, topic, continent)
        for attempt in range(3):
            try:
                resp = gemini.generate_content(prompt)
                raw  = resp.text
                logger.info(f"Gemini 콘텐츠 생성 완료 ({len(raw)}자)")
                return _parse_checklist(raw, destination, topic)
            except Exception as e:
                logger.warning(f"Gemini 호출 실패 ({attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(15 * (attempt + 1))
                else:
                    raise


# ==========================================
# 8. 가이드북 스타일 추출 (구 스토리텔링형 파이프라인 — 현재 미사용, 참고용 보존)
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

def fetch_country_facts(destination: str) -> Dict:
    """REST Countries API(무료, 키 불필요) — 통화·언어·수도 등 검증된 사실 정보.
    AI 추측 대신 실제 데이터로 기본정보표를 채워 신뢰도를 높입니다.
    """
    facts: Dict = {}
    try:
        # Gemini로 여행지→국가 매핑 (지명이 국가가 아닐 수 있으므로)
        prompt = (
            f"What country is '{destination}' located in? "
            f"Reply with ONLY the English country name (e.g. 'Vietnam', 'Japan'). No explanation."
        )
        model = genai.GenerativeModel("gemini-2.0-flash")
        resp = model.generate_content(prompt)
        country_en = resp.text.strip().strip("'\".")
        if not country_en or len(country_en) > 60:
            return facts

        r = requests.get(
            f"https://restcountries.com/v3.1/name/{quote(country_en)}",
            params={"fields": "name,currencies,languages,timezones,capital"},
            timeout=12,
        )
        if r.status_code != 200:
            return facts
        results = r.json()
        if not results:
            return facts
        c = results[0]
        currencies = c.get("currencies", {})
        if currencies:
            code, cur = next(iter(currencies.items()))
            facts["currency"] = f"{cur.get('name', code)} ({cur.get('symbol', code)})"
        languages = c.get("languages", {})
        if languages:
            facts["languages"] = ", ".join(languages.values())
        timezones = c.get("timezones", [])
        if timezones:
            facts["timezone"] = timezones[0]
        capital = c.get("capital", [])
        if capital:
            facts["capital"] = capital[0]
        facts["country_en"] = country_en
        logger.info(f"[국가정보팀] '{destination}' → {country_en}: {facts}")
    except Exception as e:
        logger.debug(f"REST Countries 조회 실패 ({destination}): {e}")
    return facts


def fetch_verified_attractions(destination: str) -> List[Dict]:
    """Google Places API(이미 보유한 GOOGLE_MAPS_KEY) — 실존 명소 검증 리스트.
    Gemini가 없는 명소를 지어내지 않도록 실제 장소명·평점·주소를 사실 근거로 제공합니다.
    """
    if not GOOGLE_MAPS_KEY:
        return []
    places: List[Dict] = []
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": f"tourist attractions in {destination}", "key": GOOGLE_MAPS_KEY},
            timeout=15,
        )
        if r.status_code != 200:
            return places
        for item in r.json().get("results", [])[:10]:
            name = item.get("name", "")
            if not name:
                continue
            places.append({
                "name": name,
                "rating": item.get("rating", ""),
                "address": item.get("formatted_address", ""),
            })
        logger.info(f"[장소검증팀] '{destination}' 검증 명소 {len(places)}건 수집")
    except Exception as e:
        logger.debug(f"Google Places 검증 조회 실패 ({destination}): {e}")
    return places


def fetch_transport_price_facts(famous: str, hidden: str) -> str:
    """SerpApi Google 검색 — {famous}↔{hidden} 구간의 실제 교통 요금 정보를 검색해 근거자료로 제공합니다.
    Gemini가 일반적인 평균치 대신 실제 검색된 시세를 기반으로 요금을 작성하도록 합니다.
    """
    if not SERPAPI_KEY:
        return ""
    snippets: List[str] = []
    queries = [
        f"{famous} to {hidden} shuttle transfer price",
        f"{famous} to {hidden} taxi fare price",
        f"{famous} to {hidden} bus price ticket",
    ]
    for q in queries:
        try:
            resp = requests.get(
                "https://serpapi.com/search",
                params={"engine": "google", "q": q, "api_key": SERPAPI_KEY, "hl": "en", "num": 5},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            for item in resp.json().get("organic_results", [])[:4]:
                snippet = item.get("snippet", "")
                title = item.get("title", "")
                if snippet:
                    snippets.append(f"[{title}] {snippet}")
        except Exception as e:
            logger.debug(f"교통 요금 검색 실패 ({q}): {e}")
    if not snippets:
        return ""
    result = "\n".join(f"- {s}" for s in snippets[:12])
    logger.info(f"[교통요금검색팀] '{famous}↔{hidden}' 검색 결과 {len(snippets)}건 수집")
    return result


def verify_place_exists(place_name: str, destination: str) -> bool:
    """개별 명소 실존 여부를 직접 검색으로 확인.
    포괄 리스트 대조 방식은 오지·정착지명을 놓치므로, 명소 단위로 직접 조회하는 것이 정확합니다.
    """
    if not GOOGLE_MAPS_KEY:
        return True  # 키 없으면 검증 불가 — 오탐 방지 위해 통과 처리
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
            params={
                "input": f"{place_name} {destination}",
                "inputtype": "textquery",
                "fields": "place_id",
                "key": GOOGLE_MAPS_KEY,
            },
            timeout=12,
        )
        if r.status_code != 200:
            return True
        return bool(r.json().get("candidates"))
    except Exception as e:
        logger.debug(f"명소 존재 검증 실패 ({place_name}): {e}")
        return True


def fetch_travel_data(destination: str) -> Dict:
    with tracer.start_as_current_span("fetch_travel_data") as span:
        span.set_attribute("destination", destination)
        data: Dict = {
            "destination": destination,
            "overview": "", "attractions": "", "food": "",
            "transport": "", "accommodation": "", "tips": "",
            "sources": [],
            "country_facts": {},
            "verified_attractions": [],
        }
        try:
            data["country_facts"] = fetch_country_facts(destination)
        except Exception as e:
            logger.debug(f"국가 정보 조회 실패 ({destination}): {e}")
        try:
            data["verified_attractions"] = fetch_verified_attractions(destination)
        except Exception as e:
            logger.debug(f"검증 명소 조회 실패 ({destination}): {e}")

        # 1순위: 공식 관광 사이트 텍스트 수집 (가장 신뢰도 높은 소스)
        try:
            official_urls = _get_official_tourism_urls(destination)
            for off_url in official_urls:
                resp = safe_get(off_url, timeout=15)
                if not resp:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = soup.get_text(" ", strip=True)
                if len(text) < 300:
                    continue
                data["overview"] = text[:3000]
                data["sources"].append(off_url)
                logger.info(f"[공식사이트] '{destination}' 텍스트 수집 성공: {off_url}")
                break
        except Exception as e:
            logger.debug(f"공식 사이트 텍스트 수집 실패 ({destination}): {e}")

        enc = quote(destination.replace(" ", "_"))
        for base in ["https://wikitravel.org/en/", "https://en.wikivoyage.org/wiki/"]:
            resp = safe_get(base + enc, timeout=15)
            if not resp:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            content = soup.select_one("#mw-content-text")
            if not content or len(content.get_text(strip=True)) < 400:
                continue
            # 공식 사이트에서 이미 개요를 확보했으면 덮어쓰지 않고 세부 섹션만 보강
            if not data["overview"]:
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
        "{d} cityscape skyline golden hour",
        "{d} scenic view travel photography",
        "{d} breathtaking vista overview",
        "{d} travel destination beauty",
        "{d} iconic view sunrise sunset",
        "{d} landscape nature",
        "{d} tourism attraction overview",
    ],
    "portrait":   [
        "{d} local people culture portrait",
        "{d} traditional culture lifestyle",
        "{d} community life people",
        "{d} festival celebration tradition",
        "{d} artisan craftsman local",
        "{d} market bazaar vendors",
        "{d} cultural heritage people",
    ],
    "attraction": [
        "{d} famous landmark heritage",
        "{d} historic monument architecture",
        "{d} UNESCO world heritage site",
        "{d} tourist attraction sightseeing",
        "{d} ancient ruins historical",
        "{d} palace temple cathedral",
        "{d} iconic building structure",
        "{d} museum gallery cultural site",
    ],
    "food":       [
        "{d} traditional food dish",
        "{d} local cuisine restaurant",
        "{d} street food market",
        "{d} authentic food culture",
        "{d} chef cooking kitchen",
        "{d} dining experience meal",
        "{d} dessert sweet local",
    ],
    "transport":  [
        "{d} airport train station transportation",
        "{d} public transit city transport",
        "{d} scenic route road journey",
        "{d} taxi bus local transport",
        "{d} ferry boat harbor port",
        "{d} city street commute",
    ],
    "tips":       [
        "{d} nature wilderness landscape",
        "{d} outdoor adventure travel",
        "{d} scenic hiking trail",
        "{d} weather season climate",
        "{d} accommodation hotel resort",
        "{d} shopping district market",
        "{d} night life entertainment",
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


def _pixabay_search(query: str, used_urls: set) -> Optional[bytes]:
    """Pixabay API — 무료 키 발급 가능 (pixabay.com/api/docs/)."""
    if not PIXABAY_KEY:
        return None
    try:
        import random
        resp = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_KEY,
                "q": query,
                "image_type": "photo",
                "orientation": "horizontal",
                "category": "travel",
                "min_width": 800,
                "per_page": 20,
                "page": random.randint(1, 3),
                "safesearch": "true",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        hits = resp.json().get("hits", [])
        random.shuffle(hits)
        for hit in hits:
            url = hit.get("largeImageURL") or hit.get("webformatURL", "")
            if not url:
                continue
            data = _fetch_url(url, used_urls, min_bytes=20000)
            if data:
                logger.info(f"[Pixabay] {query[:40]} → {url[:60]}")
                return data
    except Exception as e:
        logger.debug(f"Pixabay 오류 ({query[:30]}): {e}")
    return None


def _openverse_search(query: str, used_urls: set) -> Optional[bytes]:
    """Openverse — WordPress 오픈 이미지 검색, API 키 불필요."""
    try:
        resp = requests.get(
            "https://api.openverse.org/v1/images/",
            params={
                "q": query,
                "license_type": "commercial,modification",
                "page_size": 20,
                "format": "json",
            },
            headers={"User-Agent": "trip-auto-publisher/1.0"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        import random
        results = resp.json().get("results", [])
        random.shuffle(results)
        for item in results:
            url = item.get("url", "")
            if not url or not url.startswith("http"):
                continue
            data = _fetch_url(url, used_urls, min_bytes=15000)
            if data:
                logger.info(f"[Openverse] {query[:40]} → {url[:60]}")
                return data
    except Exception as e:
        logger.debug(f"Openverse 오류 ({query[:30]}): {e}")
    return None


def _pexels_scrape(query: str, used_urls: set) -> Optional[bytes]:
    """Pexels 웹 스크래핑 — API 키 불필요."""
    try:
        url = f"https://www.pexels.com/search/{quote(query.replace(' ', '-'))}/"
        resp = requests.get(url, headers=_HDRS, timeout=15)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for img in soup.select("article img, [class*='photo'] img, [class*='Photo'] img"):
            srcset = img.get("srcset") or img.get("data-srcset") or ""
            img_url = ""
            if srcset:
                candidates = []
                for part in srcset.split(","):
                    tokens = part.strip().split()
                    if tokens:
                        w = int(tokens[1].rstrip("w")) if len(tokens) > 1 and tokens[1].endswith("w") else 0
                        candidates.append((w, tokens[0]))
                if candidates:
                    img_url = max(candidates, key=lambda x: x[0])[1]
            if not img_url:
                img_url = img.get("src") or img.get("data-src") or ""
            if not img_url or not img_url.startswith("http"):
                continue
            if any(x in img_url.lower() for x in ["avatar", "logo", "icon", "1x1"]):
                continue
            data = _fetch_url(img_url, used_urls, min_bytes=20000)
            if data:
                logger.info(f"[Pexels스크래핑] {query[:40]} → {img_url[:60]}")
                return data
    except Exception as e:
        logger.debug(f"Pexels 스크래핑 오류 ({query[:30]}): {e}")
    return None


def _google_places_photos(destination: str, used_urls: set) -> Optional[bytes]:
    """Google Maps Places API 사진 — GOOGLE_MAPS_KEY 설정 시 사용."""
    if not GOOGLE_MAPS_KEY:
        return None
    try:
        # 1단계: 장소 검색으로 place_id + 사진 레퍼런스 획득
        find_resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
            params={
                "input": destination,
                "inputtype": "textquery",
                "fields": "place_id,photos",
                "key": GOOGLE_MAPS_KEY,
            },
            timeout=15,
        )
        if find_resp.status_code != 200:
            return None
        candidates = find_resp.json().get("candidates", [])
        if not candidates:
            return None
        photos = candidates[0].get("photos", [])
        if not photos:
            # 2단계: place_id로 상세 조회
            place_id = candidates[0].get("place_id", "")
            if place_id:
                detail_resp = requests.get(
                    "https://maps.googleapis.com/maps/api/place/details/json",
                    params={"place_id": place_id, "fields": "photos", "key": GOOGLE_MAPS_KEY},
                    timeout=15,
                )
                if detail_resp.status_code == 200:
                    photos = detail_resp.json().get("result", {}).get("photos", [])
        import random
        random.shuffle(photos)
        for photo in photos[:10]:
            ref = photo.get("photo_reference", "")
            if not ref:
                continue
            img_url = (
                f"https://maps.googleapis.com/maps/api/place/photo"
                f"?maxwidth=1200&photoreference={ref}&key={GOOGLE_MAPS_KEY}"
            )
            data = _fetch_url(img_url, used_urls, min_bytes=20000)
            if data:
                logger.info(f"[GoogleMaps] {destination} → photo_ref={ref[:20]}")
                return data
    except Exception as e:
        logger.debug(f"Google Places 오류 ({destination}): {e}")
    return None


def _wikimedia_search(query: str, used_urls: set) -> Optional[bytes]:
    """Wikimedia Commons — API 키 불필요, 무료 고화질 여행 사진."""
    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f"File:{query}",
                "gsrnamespace": 6,
                "gsrlimit": 20,
                "prop": "imageinfo",
                "iiprop": "url|size|mime",
                "iiurlwidth": 1200,
                "format": "json",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            ii = page.get("imageinfo", [{}])[0]
            mime = ii.get("mime", "")
            if "image" not in mime or "svg" in mime or "gif" in mime:
                continue
            url = ii.get("thumburl") or ii.get("url", "")
            if not url:
                continue
            data = _fetch_url(url, used_urls, min_bytes=15000)
            if data:
                logger.info(f"[Wikimedia] {query[:40]} → {url[:60]}")
                return data
    except Exception as e:
        logger.debug(f"Wikimedia 오류 ({query[:30]}): {e}")
    return None


def _wikipedia_main_image(destination: str, used_urls: set) -> Optional[bytes]:
    """Wikipedia 대표 이미지 — 여행지 이름으로 직접 조회."""
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "titles": destination,
                "prop": "pageimages",
                "pithumbsize": 1200,
                "format": "json",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            thumb = page.get("thumbnail", {})
            url = thumb.get("source", "")
            if not url:
                continue
            data = _fetch_url(url, used_urls, min_bytes=10000)
            if data:
                logger.info(f"[Wikipedia] {destination} → {url[:60]}")
                return data
    except Exception as e:
        logger.debug(f"Wikipedia 이미지 오류 ({destination}): {e}")
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
        page = random.randint(1, 6)
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
) -> Optional[Tuple[bytes, str]]:
    """포토그래픽팀 — 섹션별 정교한 쿼리로 여행 사진을 수집합니다.
    우선순위: Unsplash API → Pexels API → Bing Image Search API(키 있을 때)
    used_urls를 공유해 동일 글 내 중복 이미지를 차단합니다.
    반환값: (이미지 bytes, 매칭에 사용된 검색어) — 매칭 검증용으로 검색어를 함께 반환합니다.
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

        import random as _rnd

        # 섹션별 소스 순서를 랜덤화해서 다양성 확보 (SerpApi는 교통 요금 검색 전용으로 예약)
        api_sources_with_key = []
        if GOOGLE_MAPS_KEY:
            api_sources_with_key.append("google_places")
        _rnd.shuffle(api_sources_with_key)

        # 각 쿼리마다 순서를 다르게
        shuffled_queries = queries[:]
        _rnd.shuffle(shuffled_queries)

        for q in shuffled_queries:
            # Pexels API (키 있을 때)
            result = _pexels_search(q, orientation, used_urls)
            if result:
                span.set_attribute("source", "pexels")
                span.set_attribute("found_query", q)
                return result, q
            # Pixabay API (무료 키)
            result = _pixabay_search(q, used_urls)
            if result:
                span.set_attribute("source", "pixabay")
                span.set_attribute("found_query", q)
                return result, q
            # Openverse (API 키 불필요)
            result = _openverse_search(q, used_urls)
            if result:
                span.set_attribute("source", "openverse")
                span.set_attribute("found_query", q)
                return result, q
            # Wikimedia Commons (API 키 불필요)
            result = _wikimedia_search(q, used_urls)
            if result:
                span.set_attribute("source", "wikimedia")
                span.set_attribute("found_query", q)
                return result, q
            # Unsplash (키 있을 때)
            result = _unsplash_search(q, orientation, used_urls)
            if result:
                span.set_attribute("source", "unsplash")
                span.set_attribute("found_query", q)
                return result, q
            # Bing (키 있을 때)
            result = _bing_api_search(q, orientation, used_urls)
            if result:
                span.set_attribute("source", "bing_api")
                span.set_attribute("found_query", q)
                return result, q

        # 키 있는 고품질 소스들 (SerpApi는 교통 요금 검색 전용으로 예약 — 사진 검색엔 사용 안 함)
        for src in api_sources_with_key:
            if src == "google_places":
                result = _google_places_photos(destination, used_urls)
                if result:
                    span.set_attribute("source", "google_maps")
                    return result, destination

        # Pexels 웹 스크래핑 폴백
        for q in shuffled_queries[:3]:
            result = _pexels_scrape(q, used_urls)
            if result:
                span.set_attribute("source", "pexels_scrape")
                return result, q

        # 최후 폴백: Wikipedia 대표 이미지
        result = _wikipedia_main_image(destination, used_urls)
        if result:
            span.set_attribute("source", "wikipedia")
            return result, destination

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
    "font-size:clamp(12px,3vw,14px);font-weight:700;text-decoration:none;color:#fff;"
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
            f'<td style="padding:10px 14px;vertical-align:top;text-align:center;'
            f'{"font-weight:600;" if j==0 else ""}">'
            f'{(str(v).strip() or "-")}</td>'
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


def build_transport_classes_table(services: List[str], destination: str, cat_color: str, famous: str = "") -> str:
    """교통편 클래스 비교 테이블 HTML 생성 (Gemini로 조회, 실시간 검색 근거자료 활용)."""
    if not services or not gemini:
        return ""
    service_list = "\n".join(f"- {s}" for s in services[:3])
    price_facts = fetch_transport_price_facts(famous, destination) if famous else ""
    facts_block = (
        f"[실시간 검색 근거자료]\n{price_facts}\n위 검색 결과에 구체적 요금이 있으면 우선 반영할 것.\n\n"
        if price_facts else ""
    )
    prompt = (
        f"For the following transport services relevant to {destination}, "
        f"list the available seat/cabin classes in order from lowest to highest tier.\n"
        f"{service_list}\n\n"
        f"{facts_block}"
        f"For each service and class, output exactly 4 fields separated by '|' — no more, no fewer:\n"
        f"ServiceName|ClassName|PriceRange|KeyDifferences\n"
        f"ServiceName = the company/service name only (e.g. 'Supratours'). Do NOT add a separate vehicle-type field.\n"
        f"ClassName = the actual tier/class name (e.g. 'Confort', 'Confort Plus'), never a generic word like 'bus' or 'taxi'.\n"
        f"PriceRange = ONLY use a number if the [실시간 검색 근거자료] above contains a concrete figure for this route. "
        f"When used, write it in the ACTUAL LOCAL CURRENCY of {destination}'s country (e.g. JPY, VND, THB, MAD, EUR) — never USD — "
        f"with an approximate KRW conversion in parentheses. Base it on realistic local fares for this specific route "
        f"(consider distance, terrain, tolls) — never reuse generic global averages.\n"
        f"If the search facts do NOT cover this specific class/route, do not guess a number — write in Korean exactly: "
        f"'요금 확인 필요 (예약 시점 현지 사이트 참조)'.\n"
        f"KeyDifferences should still be filled in with real, useful detail (seating, amenities, booking method) even when price is unknown.\n"
        f"Example: Supratours|Confort|120-180 MAD (약 21,000~32,000원)|Standard seating, AC\n"
        f"Output only the data lines. No explanations. No markdown."
    )
    try:
        resp = gemini.generate_content(prompt)
        lines = [l.strip() for l in resp.text.strip().splitlines() if l.count("|") == 3]
        if not lines:
            return ""
        html = ""
        headers = ["클래스", "요금 기준 (현지통화)", "주요 차이점"]
        fallback_price = "요금 확인 필요"

        def _flush(svc: str, rows: List[Dict]) -> str:
            if not rows:
                return ""
            # 모든 클래스의 요금이 전부 확인 불가면 표 대신 한 줄 문장으로 축약 (같은 문구 반복 방지)
            if all(fallback_price in r["cells"][1] for r in rows):
                class_names = ", ".join(r["cells"][0] for r in rows)
                return (
                    f'<p style="font-size:14px;color:#64748b;margin:12px 0;">'
                    f'{svc} ({class_names}) 요금은 예약 시점에 따라 변동이 커서, 현지 공식 사이트에서 직접 확인하시길 권장합니다.</p>'
                )
            return _build_options_table(rows, f"{svc} 클래스 비교", headers, cat_color)

        current_service = None
        service_rows: List[Dict] = []
        for line in lines:
            parts = [p.strip() for p in line.split("|")]
            svc, cls_name, price, diff = parts[0], parts[1], parts[2], parts[3]
            if svc != current_service:
                html += _flush(current_service, service_rows)
                current_service = svc
                service_rows = []
            service_rows.append({"cells": [cls_name, price, diff]})
        html += _flush(current_service, service_rows)
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
        f'style="{_BTN_STYLE}background:{color};">{name} 숙소 검색</a>'
        for name, url, color in valid
    )
    return f'<div style="{_BTN_WRAP}">{btns}</div>'


def build_hotel_buttons_custom(destination: str) -> str:
    """세시간전 제휴 링크 기반 맞춤형 숙소 CTA 버튼 (Agoda · Expedia · Trip.com)."""
    hotels = [
        (AFF_AGODA,   "#e11d48", "Agoda",     f"{destination} 인기 숙소 시크릿 특가 및 남은 객실 확인"),
        (AFF_EXPEDIA, "#0c69b0", "Expedia",   f"{destination} 추천 숙소 무료 취소 가능 객실 선점"),
        (AFF_TRIP,    "#1a7abf", "Trip.com",  f"{destination} 최저가 호텔 바로 예약"),
    ]
    btns = "".join(
        f'<a href="{url}" target="_blank" rel="nofollow noopener sponsored" '
        f'style="{_BTN_STYLE}background:{color};display:block;margin:6px 0;text-align:center;">'
        f'<span style="opacity:0.75;font-size:0.85em;">{brand}</span> {label}</a>'
        for url, color, brand, label in hotels
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
        (AFF_KLOOK, "#e85d04", "Klook",    f"{tour_name} 최저가 예약"),
        (AFF_TRIP,  "#1a7abf", "Trip.com", f"{destination} 투어·액티비티 예약"),
    ]
    btns = "".join(
        f'<a href="{url}" target="_blank" rel="nofollow noopener sponsored" '
        f'style="{_BTN_STYLE}background:{color};display:block;margin:6px 0;text-align:center;">'
        f'<span style="opacity:0.75;font-size:0.85em;">{brand}</span> {label}</a>'
        for url, color, brand, label in entries
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
    transport_price_facts = fetch_transport_price_facts(famous, hidden)
    maps_embed = (
        f'<iframe src="https://maps.google.com/maps?q={quote(hidden)}&z=11&output=embed" '
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

    country_facts = data_hidden.get("country_facts") or {}
    if country_facts:
        lines = [f"국가: {country_facts.get('country_en', '')}"]
        if country_facts.get("languages"):
            lines.append(f"공용어: {country_facts['languages']}")
        if country_facts.get("currency"):
            lines.append(f"통화: {country_facts['currency']}")
        if country_facts.get("timezone"):
            lines.append(f"시차(UTC): {country_facts['timezone']}")
        if country_facts.get("capital"):
            lines.append(f"수도: {country_facts['capital']}")
        country_facts_block = "국가 기본 정보(REST Countries API 검증):\n" + "\n".join(f"- {l}" for l in lines)
    else:
        country_facts_block = "국가 기본 정보: 검증 데이터 없음 — 알고 있는 사실만 신중히 서술"

    verified = data_hidden.get("verified_attractions") or []
    if verified:
        lines = [f"{p['name']}" + (f" (평점 {p['rating']})" if p.get("rating") else "") for p in verified]
        verified_attractions_block = (
            f"{hidden} 실존 명소 목록(Google Places API 검증):\n" + "\n".join(f"- {l}" for l in lines)
        )
    else:
        verified_attractions_block = "실존 명소 목록: 검증 데이터 없음 — [수집 정보]에 있는 명소만 서술, 임의 생성 금지"

    return f"""
당신은 trip.bestwellth.org의 전문 여행 큐레이터입니다.
아래 [수집 정보]와 [가이드북 스타일]을 바탕으로 완성된 블로그 포스팅 HTML을 작성하세요.

[오늘의 여행지 컨텍스트]
오늘은 {continent_label} 특집입니다.
이 글의 실제 주제는 {hidden} 단 하나입니다. {famous}는 "{hidden}에 가려면 거쳐야 하는 관문 도시"로서 접근 경로 설명에만 짧게 등장합니다.
- {famous} (게이트웨이 도시, 한국인이 많이 검색하는 인기 도시): 본문에서 별도 섹션을 차지하지 않고, 교통 안내 문맥에서 "이 도시를 통해 들어간다" 정도로만 언급
- {hidden} (이 글의 진짜 주인공, 특정 관광지·지역 단위): 본문 전체 분량의 대부분을 차지하는 심층 탐구 대상
포스팅의 핵심 가치: "{famous} 여행 중이라면 꼭 들러야 할 {hidden}"이라는 구체적인 근교 나들이 정보 제공.
{famous}는 같은 도시가 여러 글에서 반복 등장할 수 있으므로(예: {famous}는 다른 글에서 다른 지역과도 짝지어질 수 있음),
이 글에서 {famous} 자체를 깊게 다루지 말 것 — 대신 {hidden}이라는 좁고 구체적인 장소에 집중할 것.

[분량 제한 — 반드시 준수]
- 본문 HTML 총 글자 수 5,500자 이하 (HTML 태그 포함)
- H2 섹션 최대 5개, H3 최대 3개 (전체 섹션 8개 이하)
- 표(table) 최대 2개 — 기본정보표 + 일정표만 허용
- 교통 수단 비교는 표 금지 → 카드형 div로 대체
- 맛집·숙소는 표 금지 → 간단한 리스트(이름 + 한 줄 특징 + 가격대)로 대체
- 문단(p태그) 총 20개 이하
- 캡션 제외 모든 텍스트 font-size 13px 이상 (12px 절대 금지)

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

[검증된 사실 정보 — 반드시 이 값을 그대로 사용, 임의로 다른 값 지어내기 금지]
{country_facts_block}
{verified_attractions_block}

[교통 요금 실시간 검색 근거자료 — {famous}↔{hidden} 구간]
{transport_price_facts if transport_price_facts else "검색 결과 없음 — 막연한 평균치로 지어내지 말고, 확신이 없으면 범위를 넓게 잡거나 '-'로 표기할 것"}
위 검색 결과에 구체적인 요금이 있으면 그 수치를 우선 사용하고, 정기 노선버스/공유셔틀/프라이빗 전세/택시 등 서비스 종류가 다르면 반드시 구분해서 각각 표기한다.
검색 결과가 부족해도 동네 단거리 요금을 장거리 구간에 그대로 적용하지 말 것.

[절대 금지 사항]
- 이모티콘(Emoji) 사용 전면 금지 (제목·본문 모두)
- 개인 일기·경험 형식 금지 ("저는", "제가", "다녀왔습니다" 등)
- Markdown 기호(**, ##, -, *) 본문 삽입 금지
- 수치·사실 지어내기 금지 — [검증된 사실 정보]에 값이 있으면 그 값을 그대로 사용
- [검증된 사실 정보]에 언어·통화·시차가 명시되어 있으면 기본 정보 표에 반드시 그 값을 사용
- [검증된 사실 정보]에 명소 리스트가 있으면 본문 명소 설명 시 그 실존 명소명을 우선 활용 (완전히 새로운 명소를 지어내지 않음)
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

--- 3.5. 핵심 요약 카드 (인트로 박스 바로 아래 — 첫 화면 최우선) ---
<div style="background:#0c4a6e;border-radius:16px;padding:24px 26px;margin-bottom:28px;">
  <p style="margin:0 0 14px 0;font-size:15px;font-weight:700;color:#ffffff;letter-spacing:0.08em;">핵심 3가지</p>
  <ul style="list-style:none;padding:0;margin:0;">
    <li style="display:flex;align-items:flex-start;gap:12px;margin-bottom:10px;"><span style="display:inline-block;background:{CAT_COLOR};color:#fff;font-size:13px;font-weight:800;padding:2px 8px;border-radius:4px;flex-shrink:0;margin-top:2px;">01</span><span style="font-size:15px;color:#ffffff;line-height:1.7;">[핵심 포인트 1 — 여행지 최대 매력 한 문장]</span></li>
    <li style="display:flex;align-items:flex-start;gap:12px;margin-bottom:10px;"><span style="display:inline-block;background:{CAT_COLOR};color:#fff;font-size:13px;font-weight:800;padding:2px 8px;border-radius:4px;flex-shrink:0;margin-top:2px;">02</span><span style="font-size:15px;color:#ffffff;line-height:1.7;">[핵심 포인트 2 — 이동 방법 또는 비용 핵심]</span></li>
    <li style="display:flex;align-items:flex-start;gap:12px;"><span style="display:inline-block;background:{CAT_COLOR};color:#fff;font-size:13px;font-weight:800;padding:2px 8px;border-radius:4px;flex-shrink:0;margin-top:2px;">03</span><span style="font-size:15px;color:#ffffff;line-height:1.7;">[핵심 포인트 3 — 방문 시기 또는 주의사항]</span></li>
  </ul>
</div>

{{PHOTO:featured}}

--- 4. 여행 기본 정보 표 ---
<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 0 28px 0;word-break:keep-all;">
<table style="width:100%;border-collapse:collapse;font-size:14px;min-width:280px;">
  <thead><tr style="background:#0c4a6e;color:#fff;">
    <th style="padding:11px 14px;text-align:center;font-weight:700;width:35%;">항목</th>
    <th style="padding:11px 14px;text-align:center;font-weight:700;">내용</th>
  </tr></thead>
  <tbody>
    <tr style="background:#fff;border-bottom:1px solid #e2e8f0;"><td style="padding:10px 14px;font-weight:700;color:#0f172a;text-align:center;">위치</td><td style="padding:10px 14px;color:#334155;">[국가 · 지역 · 도시명]</td></tr>
    <tr style="background:#f8fafc;border-bottom:1px solid #e2e8f0;"><td style="padding:10px 14px;font-weight:700;color:#0f172a;text-align:center;">최적 여행 시기</td><td style="padding:10px 14px;color:#334155;">[시기 + 한 줄 이유]</td></tr>
    <tr style="background:#fff;border-bottom:1px solid #e2e8f0;"><td style="padding:10px 14px;font-weight:700;color:#0f172a;text-align:center;">언어</td><td style="padding:10px 14px;color:#334155;">[공용어]</td></tr>
    <tr style="background:#f8fafc;border-bottom:1px solid #e2e8f0;"><td style="padding:10px 14px;font-weight:700;color:#0f172a;text-align:center;">통화</td><td style="padding:10px 14px;color:#334155;">[통화명 및 기호]</td></tr>
    <tr style="background:#fff;border-bottom:1px solid #e2e8f0;"><td style="padding:10px 14px;font-weight:700;color:#0f172a;text-align:center;">시차 (한국 기준)</td><td style="padding:10px 14px;color:#334155;">[UTC±X / 한국보다 N시간]</td></tr>
    <tr style="background:#f8fafc;"><td style="padding:10px 14px;font-weight:700;color:#0f172a;text-align:center;">1일 평균 예산</td><td style="padding:10px 14px;color:#334155;">[예산 범위 USD]</td></tr>
  </tbody>
</table>
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
<!-- wp:html --><div style="margin-bottom:36px;"><p style="font-size:14px;font-weight:700;color:#334155;margin-bottom:8px;">{dest} 위치</p>{maps_embed}</div><!-- /wp:html -->

[[[AD_DISPLAY]]]

--- 6. 본문 (PART 1 → 연결 브릿지 → PART 2 → 동선 → 맛집/교통/숙소/팁) ---
H2 번호 금지. 포커스 키워드는 H2 전체에서 최대 1회.
강조: <span style="background-color:{CAT_LIGHT_BG};padding:2px 6px;color:{CAT_COLOR};font-weight:700;">강조 텍스트</span>

--- 접근 경로 안내 (게이트웨이 도시는 여기서만 짧게 언급, 별도 섹션·명소 소개 없음) ---
<div style="background:{CAT_LIGHT_BG};border:1px solid {CAT_LIGHT_BORDER};border-radius:16px;padding:24px 28px;margin:0 0 48px 0;">
  <p style="margin:0 0 8px 0;font-size:13px;font-weight:800;color:{CAT_COLOR};letter-spacing:0.05em;">{famous} 여행 중이라면</p>
  <p style="margin:0;font-size:15px;color:#334155;line-height:1.8;">[{famous}에 입국/도착한다는 전제 하에, {famous}에서 {hidden}까지 이동 방법·소요 시간·비용을 구체적으로 서술. "버스로 약 X시간" 또는 "차량으로 X분" 형태로 명시. {famous} 자체의 명소는 언급하지 말 것 — 오직 이동 경로 정보만.]</p>
</div>

[[[AD_IN_ARTICLE]]]

--- PART 2. 연계 여행지 심층 탐구 ---
<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">{hidden} — 아직 많은 이들이 모르는 곳</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[{hidden} 한 줄 핵심 매력]</p>

  [도입 p태그 1~2개 — {hidden}의 특별함과 방문 가치 서술]

  아래 2개 테마로 명소를 분류하여 H3 소제목 + 세부 명소 설명으로 구성한다.
  각 명소는 한국어명(영문명) 병기. 위치·핵심 볼거리·실용정보를 p태그 1개에 압축 서술.
  명소는 전체 합산 4~6개 언급.

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">[테마1 — 예: 문화·역사 유산]</h3>
  <p>[명소A (영문명): 위치·특징·실용정보.]</p>
  <p>[명소B (영문명): 위치·특징·실용정보.]</p>
  <p>[명소C (영문명): 위치·특징·실용정보.]</p>
  {{PHOTO:attraction}}

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">[테마2 — 예: 자연·체험·로컬 명소]</h3>
  <p>[명소D (영문명): 특징·실용정보.]</p>
  <p>[명소E (영문명): 특징·실용정보.]</p>
  {{PHOTO:attraction2}}
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
  [N박M일 추천 동선을 간단한 리스트로 제시. 1일차·2일차 형태로 각 1줄씩. 표 금지.]
  <ul style="margin:12px 0;padding-left:18px;font-size:14px;color:#334155;line-height:2.0;">
    <li>[1일차: {famous} 도착 → 핵심 명소 1~2곳]</li>
    <li>[2일차: {famous} → {hidden} 이동 (수단·시간) → 명소 탐방]</li>
    <li>[3일차: {hidden} 심층 탐방 → 귀국 준비]</li>
  </ul>
</div>

[[[AD_IN_ARTICLE]]]

<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  {{PICTOGRAM:food}}
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">[맛집 제목]</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[서브 문구]</p>
  {{PHOTO:food}}
  [대표 음식 소개 p태그 1~2개 — 어떤 음식이 유명한지 간결하게]
  <ul style="margin:10px 0 16px 0;padding-left:18px;font-size:14px;color:#334155;line-height:2.0;">
    <li>[식당명 A — 대표 메뉴 · 위치 · 1인 USD X~Y]</li>
    <li>[식당명 B — 대표 메뉴 · 위치 · 1인 USD X~Y]</li>
    <li>[식당명 C — 대표 메뉴 · 위치 · 1인 USD X~Y]</li>
  </ul>
</div>

<div style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  {{PICTOGRAM:transport}}
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#0f172a;margin:8px 0;line-height:1.4;">[교통 제목]</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 16px 0;">[서브 문구]</p>
  {{PHOTO:transport}}

  [{famous} 관문 공항 안내 p태그 1개 + {famous}→{hidden} 이동 방법 안내 p태그 1개]
  [반드시 포함: {famous}에 입국할 때 이용하는 공항명과 공항코드를 명시한다.
   이후 {hidden}까지 이동하는 주요 교통수단(버스·차량·기차 등)과 소요 시간을 함께 서술한다.
   가능한 실제 노선명·터미널명·환승 지점 등을 구체적으로 언급하여 신뢰도 높은 실용 정보로 작성한다.]

  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">공항에서 시내·목적지까지</h3>
  [공항→목적지 이동 안내 p태그 1개]

  [교통수단 카드 작성 규칙 — 반드시 준수]
  - 교통수단명은 "셔틀(Shuttle)"처럼 모호한 명칭 금지. 현지 실제 운영 방식·회사명으로 구체적으로 분류할 것.
    예: 현지 정기 대중교통(실제 버스/기차 노선명 또는 운영사명), 도어투도어 프라이빗 트랜스퍼(사전예약), Uber/Grab 등 현지 주력 승차공유 앱, 일반 미터기 택시, 렌터카 등.
  - 각 수단마다 실제 이용 방법(승차장 위치, 예약 방식, 운행 시간대·배차 간격)을 최대한 구체적으로 서술해 실용성을 높인다.
  - 요금은 [실시간 검색 근거자료]에 구체적 수치가 있을 때만 현지 통화(JPY, VND, THB, EUR, AUD 등) + 괄호 안 원화(KRW) 환산으로 표기한다. USD 등 임의 통화 사용 금지.
  - 검색 근거자료에 해당 구간의 신뢰할 만한 요금 정보가 없으면 숫자를 절대 추측하지 말고, 비용 칸에 정확히 "요금 확인 필요 (예약 시점 현지 사이트 참조)"라고만 쓴다. 대신 이동 방법·소요시간·이용 팁은 평소처럼 충실히 작성한다.
  - 단거리 동네 요금이나 막연한 평균치를 장거리 이동에 적용하지 말 것.
  - 직접 경험한 것처럼 서술 금지("~해봤다" 등). 다양한 공개 정보를 종합한 객관적 정보 큐레이션 톤 유지.

  <div style="display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 20px 0;">
    [교통수단별 카드 2~3개 — 아래 형식 반복]
    <div style="flex:1;min-width:140px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;">
      <p style="margin:0 0 6px 0;font-size:14px;font-weight:700;color:#0f172a;">[구체적 교통수단명]</p>
      <p style="margin:0 0 4px 0;font-size:13px;color:#64748b;">소요 [X분/시간]</p>
      <p style="margin:0 0 4px 0;font-size:13px;color:#64748b;">비용 [현지통화 금액 (약 KRW 환산금액) 또는 "요금 확인 필요 (예약 시점 현지 사이트 참조)"]</p>
      <p style="margin:0;font-size:13px;color:#94a3b8;">[한 줄 특징 — 승차장·예약 방법·운행 시간대 등 구체적으로]</p>
    </div>
  </div>

  {{TRANSPORT_CLASSES_TABLE}}
  {{TRANSPORT_BUTTONS}}

  {{PICTOGRAM:accommodation}}
  <h3 style="font-size:clamp(15px,2vw,17px);font-weight:700;color:#0f172a;margin:28px 0 12px 0;">[숙소 소제목]</h3>
  [숙소 추천 지구 p태그 1개 — 어느 동네에 묵을지 한 문장]
  <ul style="margin:10px 0 16px 0;padding-left:18px;font-size:14px;color:#334155;line-height:2.0;">
    <li>[숙소명 A — 유형(호텔/게스트하우스 등) · 특징 한 줄 · 1박 USD X~Y]</li>
    <li>[숙소명 B — 유형 · 특징 한 줄 · 1박 USD X~Y]</li>
    <li>[숙소명 C — 유형 · 특징 한 줄 · 1박 USD X~Y]</li>
  </ul>
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
  [실전 팁 p태그 1~2개 — 현지 주의사항·환전·통신 등 핵심만]
  <div style="background:{CAT_LIGHT_BG};border-left:4px solid {CAT_COLOR};padding:14px 18px;border-radius:0 12px 12px 0;margin:20px 0 0 0;">
    <p style="margin:0 0 8px 0;font-size:13px;font-weight:700;color:{CAT_COLOR};letter-spacing:0.05em;">체크리스트</p>
    <ul style="margin:0;padding-left:18px;font-size:14px;color:#334155;line-height:1.9;">
      <li>[팁 1]</li>
      <li>[팁 2]</li>
      <li>[팁 3]</li>
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
- 이 글의 실제 주제는 {hidden}(특정 관광지·지역)이다. {famous}(게이트웨이 도시)는 "{famous} 여행 중 가볼 만한 곳"이라는 맥락으로만 언급 — 두 지명을 대등하게 병렬 나열하지 말 것 (예: "{famous}와 {hidden}" 형태 금지)
- 지명 표기는 반드시 "한글명(영문명)" 형식으로 일관되게 통일할 것. 매번 같은 지명은 같은 표기 방식을 유지 (한 번은 영어만, 한 번은 한글만 쓰는 등 혼용 금지)
  예: "닛코(Nikko)", "아마노하시다테(Amanohashidate)" — {famous}, {hidden} 모두 이 형식 적용
- "여행 완전 정복", "총정리", "가이드" 같은 정보성 표현 금지
- 강한 후킹 필수: {hidden}의 가장 경이롭거나 압도적인 장면(풍경·순간·감정)을 한 장면으로 떠올리게 하는 감성적 문구를 사용해,
  그 구절만 보고도 클릭하고 싶어지게 만들 것. 과장된 거짓 정보나 낚시성 문구(내용과 무관한 자극적 표현)는 금지 — 실제 본문 내용과 반드시 일치해야 함
- "너머", "이끄는", "감춰둔", "숨겨진", "한 발 더" 같은 상투적 연결어를 매번 반복하지 말 것 — 아래 서로 다른 문형 중 이번 글에는 아직 안 써본 것을 골라 변주할 것 (모두 {famous}는 맥락, {hidden}이 주인공인 구조)
  1) 여행 중 발견형: "{famous} 여행 중이라면, 닛코(Nikko)에 들러야 하는 이유"
  2) 근교형: "{famous}에서 2시간, 닛코(Nikko)의 붉은 다리"
  3) 감각·계절형: "닛코(Nikko), 단풍이 산 전체를 태우는 곳"
  4) 동사형: "닛코(Nikko)를 걷다, {famous}와는 다른 하루"
  5) 한 줄 정의형: "닛코(Nikko), {famous} 근교에 숨은 신사 마을"
  6) 발견 서사형: "{famous}만 보고 왔다면 놓친 곳, 닛코(Nikko)"
  7) 경이 장면 후킹형: "폭포와 삼나무 숲 사이, 닛코(Nikko)의 아침"
- 30자 이내로 간결하게
[/TITLE]
[COUNTRY_KR]{famous}가 속한 국가명을 한국어로 (최대 6자, 예: 태국, 모로코, 뉴질랜드)[/COUNTRY_KR]
[FOCUS_KW]3~4단어 한국어 롱테일 키워드[/FOCUS_KW]
[META_DESC]130~155자 메타 설명 — 반드시 자연스러운 한국어 문장으로만 작성. HTML태그·이미지출처·URL·특수기호 절대 포함 금지[/META_DESC]
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
                "TICKET_URLS", "TRANSPORT_URLS", "COUNTRY_KR"]:
        body = re.sub(rf'\[{tag}\].*?\[/{tag}\]\n?', '', body, flags=re.DOTALL)
    # Gemini가 ```html ... ``` 코드블록으로 감싸는 경우 제거
    body = re.sub(r'^```(?:html)?\s*\n?', '', body.strip(), flags=re.IGNORECASE)
    body = re.sub(r'\n?```\s*$', '', body, flags=re.IGNORECASE)
    body = body.strip()

    for key in _PICTOGRAMS:
        body = body.replace(f'{{PICTOGRAM:{key}}}', pictogram_html(key))
    body = re.sub(r'<a(?![^>]*\brel=)[^>]*>(.*?)</a>', r'\1', body, flags=re.DOTALL)  # Gemini 생성 링크만 제거, rel= 있는 버튼은 유지

    # 광고 플레이스홀더 교체: 지도 아래, 본문 1 아래, 참고자료 아래
    # 광고 스크립트는 wp:html 블록으로 감싸 wpautop의 자동 줄바꿈(<br> 삽입)이
    # <script> 태그 내부를 훼손하지 않도록 보호합니다.
    body = body.replace('[[[AD_DISPLAY]]]', f'<!-- wp:html -->{AD_DISPLAY}<!-- /wp:html -->')
    body = body.replace('[[[AD_IN_ARTICLE]]]', f'<!-- wp:html -->{AD_IN_ARTICLE}<!-- /wp:html -->')
    body = body.replace('[[[AD_AUTORELAXED]]]', f'<!-- wp:html -->{AD_AUTORELAXED}<!-- /wp:html -->')

    raw_title   = ex("TITLE",      f"{dest} 여행 가이드 — 명소·맛집·교통 총정리")
    # Gemini가 지시를 무시하고 영문 지명을 괄호로 덧붙이는 경우 강제 제거 (예: "자이언 내로우즈 (Zion Narrows)")
    raw_title   = re.sub(r'\s*\([A-Za-z][A-Za-z\s\-\.\']{1,40}\)\s*$', '', raw_title).strip()
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
            # Content-Disposition 헤더는 latin-1만 허용 — 한글 등 비-ASCII 파일명(예: "페루_mid_...")이
            # 그대로 들어가면 requests가 헤더 인코딩에 실패해 업로드 자체가 조용히 죽는다.
            # 헤더용 파일명만 ASCII로 안전하게 치환하고(SEO에 영향 없음, alt_text는 별도 JSON 바디로 전송됨),
            # 비-ASCII 문자가 사라져 알아볼 수 없게 되면 타임스탬프 기반 이름으로 대체한다.
            safe_filename = re.sub(r'[^A-Za-z0-9_.\-]', '', filename)
            if not safe_filename.strip('_.-'):
                safe_filename = f"trip_{int(time.time())}.{ext if ext != 'jpeg' else 'jpg'}"
            headers = {
                "Authorization": _wp_auth()["Authorization"],
                "Content-Disposition": f'attachment; filename="{safe_filename}"',
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


def _wrap_html_blocks(body: str) -> str:
    """테이블을 overflow-x:auto wrapper + wp:html 블록으로 감싸 모바일 깨짐·Gutenberg 스타일 손실 방지."""
    import re as _re
    # 이미 overflow wrapper가 있는 표는 건너뜀
    def _wrap_table(m: "re.Match") -> str:
        table_html = m.group(1)
        if 'overflow-x:auto' in m.group(0)[:50]:
            return m.group(0)
        wrapped = (
            f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;word-break:keep-all;">'
            f'{table_html}</div>'
        )
        return f'<!-- wp:html -->{wrapped}<!-- /wp:html -->'

    body = _re.sub(r'(<table[\s\S]*?</table>)', _wrap_table, body, flags=_re.IGNORECASE)

    # <iframe>도 wpautop이 <p>로 잘못 감싸 태그를 깨뜨리는 걸 방지 (구글 지도 등)
    def _wrap_iframe(m: "re.Match") -> str:
        if 'wp:html' in body[max(0, m.start() - 20):m.start()]:
            return m.group(0)
        return f'<!-- wp:html -->{m.group(0)}<!-- /wp:html -->'

    body = _re.sub(r'<iframe[\s\S]*?</iframe>', _wrap_iframe, body, flags=_re.IGNORECASE)
    return body


def wp_publish(content: Dict, media_id: Optional[int], cat_id: Optional[int]) -> Dict:
    with tracer.start_as_current_span("wp_publish") as span:
        payload = {
            "title":   content["title"],
            "content": _wrap_html_blocks(content["body"]),
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
    """실용정보/체크리스트형(trip.com 스타일) 자동 발행 파이프라인."""
    with tracer.start_as_current_span("run") as root:
        t0 = datetime.now(timezone.utc)
        logger.info("=== trip.bestwellth.org 자동화 시작 (실용정보 체크리스트형) ===")
        send_telegram("trip.bestwellth.org 여행 블로그 자동화 시작")

        # Step 1: 이미 발행된 목적지+주제 조합 수집 (중복 방지용)
        published_set = wp_get_published_destinations()
        continent = get_today_continent()
        topics = fetch_practical_topics(published=published_set)

        candidates = [
            t for t in topics
            if not is_topic_already_published(t["destination"], t["topic"], published_set)
        ]
        if not candidates:
            logger.warning("모든 후보 조합이 이미 발행됨 — 중복 허용하고 전체 목록으로 진행")
            candidates = topics

        send_telegram(
            f"오늘의 대륙: {continent}\n\n주제 후보:\n"
            + "\n".join(f"{i}. {c['destination']} | {c['topic']}" for i, c in enumerate(candidates, 1))
        )

        chosen = candidates[0]
        destination, topic = chosen["destination"], chosen["topic"]
        root.set_attribute("destination", destination)
        root.set_attribute("topic", topic)
        root.set_attribute("continent", continent)

        # Step 2: Gemini 콘텐츠 생성
        try:
            content = generate_checklist_content(destination, topic, continent)
        except Exception as e:
            send_telegram(f"자동화 실패: 콘텐츠 생성 {e}")
            return

        logger.info(f"제목: {content['title']}")

        # Step 2.5: 숙소·투어·준비물 제휴 링크 — 사이트가 이미 연동해 둔
        # Agoda/Expedia/Trip.com/Klook/쿠팡파트너스 링크를 문맥에 맞는 섹션에 삽입.
        # Gemini가 프롬프트 지시대로 각 섹션 안에 흩어 넣지만, 혹시 누락하면
        # 끝에라도 반드시 노출되도록 fallback을 둔다.
        hotel_btns = build_hotel_buttons_custom(destination)
        top_tour = _get_top_tour(destination, content.get("meta_desc", topic))
        tour_btns = build_tour_buttons(destination, top_tour)
        coupang_btn = "" if not COUPANG_LINK else (
            f'<div style="margin:20px 0;padding:20px 24px;background:#fff7ed;'
            f'border:1px solid #fed7aa;border-radius:16px;">'
            f'<p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#ea580c;letter-spacing:0.05em;">'
            f'{destination} 여행 준비물</p>'
            f'<p style="margin:0 0 16px 0;font-size:14px;color:#78350f;line-height:1.7;">'
            f'출발 전 챙겨야 할 필수 아이템을 한곳에서 확인할 수 있습니다.</p>'
            f'<a href="{COUPANG_LINK}" target="_blank" rel="nofollow sponsored" '
            f'style="display:inline-block;background:#ea580c;color:#fff;font-size:14px;'
            f'font-weight:700;padding:10px 22px;border-radius:8px;text-decoration:none;">'
            f'여행 필수템 보러가기</a>'
            f'</div>'
        )

        for placeholder, html_block in (
            ("{HOTEL_BUTTONS}", hotel_btns),
            ("{TOUR_BUTTONS}", tour_btns),
            ("{COUPANG_BLOCK}", coupang_btn),
        ):
            if placeholder in content["body"]:
                content["body"] = content["body"].replace(placeholder, html_block)
            elif html_block:
                # Gemini가 지침을 놓친 경우를 대비한 안전망 — 본문 끝에라도 반드시 노출
                content["body"] += html_block

        used_urls: set = set()
        today = datetime.now().strftime("%Y%m%d")

        _KEYWORD_STYLE = "margin-top:6px;font-size:12px;color:#94a3b8;text-align:center;"

        def _keyword_caption(kw: str) -> str:
            return f'<figcaption style="{_KEYWORD_STYLE}">{kw}</figcaption>'

        # Step 3: 중간 사진 1장만 — 전체 섹션 중 가운데 섹션에만 이미지를 넣고
        # 나머지 섹션의 사진 플레이스홀더는 비워서 대표사진+중간사진 2장 구성으로 정리.
        # fetch_travel_image()는 (이미지 bytes, 매칭에 쓰인 검색어) 튜플을 반환한다.
        mid_idx = (len(content["sections"]) + 1) // 2 if content["sections"] else 0
        for idx, section in enumerate(content["sections"], start=1):
            placeholder = f"{{PHOTO:section_{idx}}}"
            if placeholder not in content["body"]:
                continue
            if idx != mid_idx:
                content["body"] = content["body"].replace(placeholder, "")
                continue
            try:
                pair = fetch_travel_image(
                    destination, orientation="landscape",
                    query=section["query"], section="general", used_urls=used_urls,
                )
                if not pair:
                    pair = fetch_travel_image(destination, orientation="landscape", used_urls=used_urls)
                if pair:
                    img, kw = pair
                    try:
                        img = crop_to_ratio(img, width=900, height=500)
                    except Exception:
                        pass
                    fname = f"{destination.lower().replace(' ', '_')}_mid_{today}.jpg"
                    media = wp_upload_image(img, fname, alt=f"{destination} {section['heading']}")
                    if media and media.get("url"):
                        html = (
                            f'<figure style="margin:16px 0 20px;text-align:center;">'
                            f'<img src="{media["url"]}" alt="{destination} {section["heading"]}" '
                            f'style="width:100%;max-width:900px;height:auto;border-radius:12px;object-fit:cover;" />'
                            f'{_keyword_caption(kw)}'
                            f'</figure>'
                        )
                        content["body"] = content["body"].replace(placeholder, html)
                        continue
            except Exception as e:
                logger.warning(f"중간 사진 실패 ({section['heading']}): {e}")
            content["body"] = content["body"].replace(placeholder, "")

        # Step 4: 대표(썸네일) 이미지 — 섹션과 중복되지 않는 별도 이미지
        media_id = None
        try:
            featured_query = content["sections"][0]["query"] if content["sections"] else destination
            featured_pair = fetch_travel_image(
                destination, orientation="landscape",
                query=featured_query, section="featured", used_urls=used_urls,
            )
            if featured_pair:
                featured_raw, _ = featured_pair
                featured_crop = crop_to_ratio(featured_raw, width=1200, height=675)
                fname = f"{destination.lower().replace(' ', '_')}_featured_{today}.jpg"
                media_result = wp_upload_image(featured_crop, fname, alt=f"{destination} {topic}")
                if media_result:
                    media_id = media_result.get("id")
        except Exception as e:
            logger.warning(f"대표 이미지 실패: {e}")

        # Step 5: 카테고리 (지역별 자동 분류)
        region = classify_region(destination)
        logger.info(f"지역 카테고리: {destination} → {region}")
        cat_id = wp_get_or_create_category(region)

        # Step 6: WordPress 발행
        try:
            wp_result = wp_publish(content, media_id, cat_id)
            post_url = wp_result.get("link", "")
        except Exception as e:
            send_telegram(f"자동화 실패: WordPress 발행 {e}")
            return

        elapsed = int((datetime.now(timezone.utc) - t0).total_seconds())
        summary = (
            f"<b>trip.bestwellth.org 자동 발행 완료</b>\n\n"
            f"대륙: {continent}\n"
            f"주제: {destination} | {topic}\n"
            f"제목: {content['title']}\n"
            f"URL: {post_url}\n"
            f"소요: {elapsed}초"
        )
        logger.info(summary.replace("<b>", "").replace("</b>", ""))
        send_telegram(summary)
        root.set_attribute("post_url", post_url)
        root.set_attribute("elapsed_seconds", elapsed)


def run_legacy_storytelling():
    """구 스토리텔링형(게이트웨이 도시+특정 관광지) 파이프라인 — 현재 미사용, 참고/롤백용 보존."""
    with tracer.start_as_current_span("run_legacy_storytelling") as root:
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
                content.get("transport_services", []), selected, CAT_COLOR, content.get("famous", "")
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

        def _pick_from_pool_or_api(section: str, orientation: str = "landscape", query: str = "") -> Optional[Tuple[bytes, str]]:
            """공식 사이트 풀에서 먼저 꺼내고 없으면 API로 fallback. (이미지, 검색어) 반환."""
            # featured·attraction·tips 섹션은 공식 풀 우선 사용
            if official_img_pool and section in ("featured", "attraction", "tips"):
                img = official_img_pool.pop(0)
                logger.info(f"[공식사이트풀] {section} 이미지 사용")
                return img, f"{selected} 공식 사이트"
            return fetch_travel_image(selected, orientation=orientation, section=section,
                                      query=query, used_urls=used_urls)

        # KEYWORD_STYLE: 사진 아래 검색어를 작게 표시 (매칭 불일치 시 수동 교체용)
        _KEYWORD_STYLE = (
            "margin-top:6px;font-size:12px;color:#94a3b8;text-align:center;"
        )

        def _keyword_caption(kw: str) -> str:
            return f'<figcaption style="{_KEYWORD_STYLE}">{kw}</figcaption>'

        img_landscape_pair = _pick_from_pool_or_api("featured", orientation="landscape")
        img_portrait_pair  = fetch_travel_image(selected, orientation="portrait", section="portrait", used_urls=used_urls)

        # Pinterest용 2:3 세로 이미지 2장 수집
        pin_pair1 = fetch_travel_image(selected, orientation="portrait", section="attraction", used_urls=used_urls)
        pin_pair2 = fetch_travel_image(selected, orientation="portrait", section="tips",       used_urls=used_urls)

        img_landscape, kw_landscape = img_landscape_pair if img_landscape_pair else (None, "")
        img_portrait,  kw_portrait  = img_portrait_pair  if img_portrait_pair  else (None, "")
        pin_img1, pin_kw1 = pin_pair1 if pin_pair1 else (None, "")
        pin_img2, pin_kw2 = pin_pair2 if pin_pair2 else (None, "")

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
                        f'{_keyword_caption(kw_landscape)}'
                        f'</figure>'
                    )
                    insert_pos = content["body"].find("</div>")
                    if insert_pos != -1:
                        insert_pos += 6
                        content["body"] = content["body"][:insert_pos] + photo_html + content["body"][insert_pos:]

        # Step 6.3: Pinterest 2:3 세로 이미지 2장 업로드 → {PINTEREST_IMAGES} 교체
        pin_html = ""
        pin_uploaded = []
        for idx, (pin_raw, pin_kw) in enumerate([(pin_img1, pin_kw1), (pin_img2, pin_kw2)], start=1):
            if not pin_raw:
                continue
            try:
                pin_cropped = crop_to_ratio(pin_raw, width=600, height=900)
            except Exception:
                pin_cropped = pin_raw
            pin_fname = f"{selected.lower().replace(' ', '_')}_pin{idx}_{today}.jpg"
            pin_media = wp_upload_image(pin_cropped, pin_fname, alt=f"{selected} 여행 {idx}")
            if pin_media and pin_media.get("url"):
                pin_uploaded.append((pin_media["url"], pin_kw))

        if len(pin_uploaded) >= 2:
            pin_html = (
                f'<figure style="margin:24px 0;">'
                f'<div style="display:flex;gap:12px;">'
                f'<img src="{pin_uploaded[0][0]}" alt="{selected} 명소 1" '
                f'style="aspect-ratio:2/3;object-fit:cover;border-radius:12px;flex:1;min-width:0;width:100%;" />'
                f'<img src="{pin_uploaded[1][0]}" alt="{selected} 명소 2" '
                f'style="aspect-ratio:2/3;object-fit:cover;border-radius:12px;flex:1;min-width:0;width:100%;" />'
                f'</div>'
                f'<figcaption style="{_KEYWORD_STYLE}">검색어: {pin_uploaded[0][1]} / {pin_uploaded[1][1]}</figcaption>'
                f'</figure>'
            )
        elif len(pin_uploaded) == 1:
            pin_html = (
                f'<figure style="margin:24px auto;max-width:320px;text-align:center;">'
                f'<img src="{pin_uploaded[0][0]}" alt="{selected} 명소" '
                f'style="aspect-ratio:2/3;object-fit:cover;width:100%;border-radius:12px;" />'
                f'{_keyword_caption(pin_uploaded[0][1])}'
                f'</figure>'
            )
        content["body"] = content["body"].replace("{PINTEREST_IMAGES}", pin_html)

        # Step 6.5: 섹션별 이미지 수집·업로드 후 플레이스홀더 교체
        # PHOTO:famous는 유명 여행지 이름으로 별도 검색
        # {PHOTO:featured}는 {PHOTO:famous}와 동일 처리 (첫 화면 대표 이미지)
        content["body"] = content["body"].replace("{PHOTO:featured}", "{PHOTO:famous}")

        if "{PHOTO:famous}" in content["body"]:
            try:
                # 이제 글의 실제 주인공은 hidden(특정 관광지)이므로 대표 이미지도 hidden 기준으로 검색
                famous_pair = fetch_travel_image(selected, orientation="landscape", section="attraction", used_urls=used_urls)
                if famous_pair:
                    famous_img, famous_kw = famous_pair
                    try:
                        famous_img = crop_to_ratio(famous_img, width=900, height=500)
                    except Exception:
                        pass
                    famous_fname = f"{selected.lower().replace(' ', '_')}_main_{today}.jpg"
                    famous_media = wp_upload_image(famous_img, famous_fname, alt=f"{selected} 여행")
                    if famous_media and famous_media.get("url"):
                        famous_html = (
                            f'<figure style="margin:20px 0 24px;text-align:center;">'
                            f'<img src="{famous_media["url"]}" alt="{selected} 여행" '
                            f'style="width:100%;max-width:900px;height:auto;border-radius:12px;object-fit:cover;" />'
                            f'{_keyword_caption(famous_kw)}'
                            f'</figure>'
                        )
                        content["body"] = content["body"].replace("{PHOTO:famous}", famous_html)
            except Exception as e:
                logger.warning(f"대표 이미지 실패: {e}")
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
                    sec_pair = None
                    for tq in transport_queries:
                        sec_pair = fetch_travel_image(selected, orientation="landscape", query=tq, section="transport", used_urls=used_urls)
                        if sec_pair:
                            logger.info(f"[교통 이미지] 쿼리 성공: '{tq}'")
                            break
                else:
                    sec_pair = _pick_from_pool_or_api(api_section, orientation="landscape")
                if sec_pair:
                    sec_img, sec_kw = sec_pair
                    try:
                        sec_img = crop_to_ratio(sec_img, width=900, height=500)
                    except Exception:
                        pass
                    sec_fname = f"{selected.lower().replace(' ', '_')}_{section_key}_{today}.jpg"
                    sec_media = wp_upload_image(sec_img, sec_fname, alt=f"{selected} {section_key}")
                    if sec_media and sec_media.get("url"):
                        sec_url = sec_media["url"]
                        sec_html = (
                            f'<figure style="margin:20px 0 24px;text-align:center;">'
                            f'<img src="{sec_url}" alt="{selected} {section_key}" '
                            f'style="width:100%;max-width:900px;height:auto;border-radius:12px;object-fit:cover;" />'
                            f'{_keyword_caption(sec_kw)}'
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
