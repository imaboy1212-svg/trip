"""
발행된 글들의 교통수단·요금 정보만 추출하는 점검 스크립트.
각 글의 "공항에서 시내" 카드형 교통수단 정보와 클래스 비교 표를 뽑아서
사람이 실제 시세와 대조해 수정할 수 있도록 정리된 텍스트로 출력합니다.
자동 수정은 하지 않습니다 — 확인용 리포트만 생성합니다.
"""
import re
from bs4 import BeautifulSoup

from verify_posts import get_all_published_posts, extract_destination_from_title


def extract_transport_cards(body_html: str):
    """flex 카드형 교통수단 블록(수단명/소요/비용/특징)을 추출."""
    soup = BeautifulSoup(body_html, "html.parser")
    cards = []
    # 카드는 flex-wrap 컨테이너 안의 하위 div들 — 특징적으로 "소요"로 시작하는 p를 포함
    for div in soup.find_all("div"):
        ps = div.find_all("p", recursive=False)
        if len(ps) >= 2:
            texts = [p.get_text(" ", strip=True) for p in ps]
            joined = " | ".join(texts)
            if any(t.startswith("소요") for t in texts) or any(t.startswith("비용") for t in texts):
                cards.append(joined)
    return cards


def extract_class_tables(body_html: str):
    """클래스 비교 표(제목 + 행 데이터)를 추출."""
    soup = BeautifulSoup(body_html, "html.parser")
    tables = []
    for h4 in soup.find_all("h4"):
        title = h4.get_text(strip=True)
        if "클래스 비교" not in title and "등급" not in title:
            continue
        table = h4.find_next_sibling("table")
        if not table:
            continue
        rows = []
        for tr in table.select("tbody tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            tables.append((title, rows))
    return tables


def main():
    posts = get_all_published_posts(limit=30)
    print(f"점검 대상: {len(posts)}개 글\n{'='*70}")
    for post in posts:
        title = post["title"]["rendered"] if isinstance(post["title"], dict) else post["title"]
        body = post["content"]["rendered"] if isinstance(post["content"], dict) else post["content"]
        dest = extract_destination_from_title(title)

        cards = extract_transport_cards(body)
        tables = extract_class_tables(body)

        if not cards and not tables:
            continue

        print(f"\n{'='*70}")
        print(f"[{dest}] {title}")
        print(f"URL: {post['link']}")
        if cards:
            print("\n  -- 공항→목적지 카드 --")
            for c in cards:
                print(f"  {c}")
        if tables:
            print("\n  -- 클래스 비교 표 --")
            for t_title, rows in tables:
                print(f"  {t_title}")
                for r in rows:
                    print(f"    {r}")


if __name__ == "__main__":
    main()
