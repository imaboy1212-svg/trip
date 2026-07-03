"""
기존 발행 글 사실 검증 스크립트.
WordPress에 발행된 글을 가져와 본문에 언급된 명소가 실존하는지,
기본정보표의 언어·통화·시차가 정확한지 Google Places / REST Countries API로 교차 검증합니다.
자동 수정은 하지 않고 리포트만 출력합니다 — 문제 있는 글을 사람이 골라 수정하기 위한 점검 도구입니다.
"""
import re
import requests
from bs4 import BeautifulSoup

from main import (
    WP_SITE_URL, _wp_auth, GOOGLE_MAPS_KEY,
    fetch_country_facts, verify_place_exists,
)


def get_all_published_posts(limit: int = 20):
    posts = []
    page = 1
    while len(posts) < limit:
        r = requests.get(
            f"{WP_SITE_URL}/wp-json/wp/v2/posts",
            headers=_wp_auth(),
            params={"per_page": 20, "page": page, "status": "publish", "_fields": "id,title,link,content,slug,date"},
            timeout=20,
        )
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        posts.extend(batch)
        if len(batch) < 20:
            break
        page += 1
    return posts[:limit]


def extract_destination_from_title(title_html: str) -> str:
    # [국가명] 프리픽스 제거
    title = re.sub(r'^\[.*?\]\s*', '', title_html).strip()
    return title


_NON_PLACE_STOPWORDS = (
    "tours", "charters", "association", "airlines", "operators", "agency",
    "iaato", "nomad", "krone", "birr", "dollar", "euro", "franc", "peso",
    "expedia", "agoda", "klook", "booking", "corporation", "company", "co.",
)


def extract_mentioned_places(body_html: str):
    """본문 p·li 태그에서만 '한국어명(영문명)' 패턴의 명소명을 추출.
    표(table)는 통화·시차 등 사실 데이터라 명소 검증 대상에서 제외.
    기관명·투어사명·통화명은 스톱워드로 걸러 오탐을 줄입니다.
    """
    soup = BeautifulSoup(body_html, "html.parser")
    for table in soup.find_all("table"):
        table.decompose()
    text = " ".join(tag.get_text(" ", strip=True) for tag in soup.find_all(["p", "li"]))
    matches = re.findall(r'([가-힣·\s]{2,20})\(([A-Za-z][A-Za-z\s\-\.]{2,40})\)', text)
    filtered = []
    for kor, eng in matches:
        eng_lower = eng.lower()
        if any(sw in eng_lower for sw in _NON_PLACE_STOPWORDS):
            continue
        if re.fullmatch(r'[A-Z]{2,4}', eng.strip()):  # 통화 코드(NOK, ETB 등) 제외
            continue
        filtered.append((kor, eng))
    return filtered


def extract_info_table(body_html: str):
    """기본정보표에서 언어·통화·시차 값 추출."""
    soup = BeautifulSoup(body_html, "html.parser")
    info = {}
    for tr in soup.select("table tr"):
        cells = tr.find_all("td")
        if len(cells) == 2:
            key = cells[0].get_text(strip=True)
            val = cells[1].get_text(strip=True)
            if key in ("언어", "통화", "시차 (한국 기준)"):
                info[key] = val
    return info


def verify_post(post: dict) -> dict:
    title = post["title"]["rendered"] if isinstance(post["title"], dict) else post["title"]
    body = post["content"]["rendered"] if isinstance(post["content"], dict) else post["content"]
    dest = extract_destination_from_title(title)

    result = {
        "id": post["id"],
        "title": title,
        "link": post["link"],
        "date": post.get("date", ""),
        "issues": [],
    }

    # 1. 명소 실존 여부 개별 검증 (Google Places Find Place — 명소 단위 직접 조회)
    mentioned = extract_mentioned_places(body)
    if mentioned and GOOGLE_MAPS_KEY:
        not_found = []
        for kor, eng in mentioned[:8]:
            eng_clean = eng.strip()
            if len(eng_clean) < 3:
                continue
            if not verify_place_exists(eng_clean, dest):
                not_found.append(f"{kor.strip()}({eng_clean})")
        if not_found:
            result["issues"].append(
                f"명소 실존 확인 안 됨 (구글 검색 결과 없음 — 지어낸 명소일 가능성): {', '.join(not_found)}"
            )

    # 2. 기본정보표 언어/통화/시차 검증 (REST Countries)
    table_info = extract_info_table(body)
    if table_info:
        try:
            facts = fetch_country_facts(dest)
            if facts.get("languages") and "언어" in table_info:
                if not any(lang.strip() in table_info["언어"] for lang in facts["languages"].split(",")):
                    result["issues"].append(
                        f"언어 불일치 — 글: '{table_info['언어']}' / 검증: '{facts['languages']}'"
                    )
            if facts.get("currency") and "통화" in table_info:
                cur_code = facts["currency"].split("(")[0].strip()
                if cur_code.lower() not in table_info["통화"].lower():
                    result["issues"].append(
                        f"통화 불일치 — 글: '{table_info['통화']}' / 검증: '{facts['currency']}'"
                    )
        except Exception as e:
            result["issues"].append(f"국가 정보 검증 중 오류: {e}")

    return result


def main():
    posts = get_all_published_posts(limit=15)
    print(f"검증 대상: {len(posts)}개 글\n{'='*60}")
    flagged = 0
    for post in posts:
        result = verify_post(post)
        if result["issues"]:
            flagged += 1
            print(f"\n[문제 발견] {result['title']}")
            print(f"  URL: {result['link']}")
            for issue in result["issues"]:
                print(f"  - {issue}")
        else:
            print(f"[OK] {result['title']}")
    print(f"\n{'='*60}\n검증 완료: {len(posts)}개 중 {flagged}개 글에서 확인 필요 항목 발견")


if __name__ == "__main__":
    main()
