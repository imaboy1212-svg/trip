"""
일회성 유틸: 기존 체크리스트형 임시글(draft)을 최신 파이프라인 기준으로
다시 생성해 같은 post id에 덮어씁니다. (버튼 분산배치, 사진 2장 구성,
아이콘 제거, 7~9섹션 분량 확대 등 최근 수정사항 반영)

정기 자동화(main.py)와는 별개의 일회성 스크립트입니다 — 스케줄에 등록하지 않습니다.
"""
import time
import logging
from datetime import datetime

from main import (
    generate_checklist_content, fetch_travel_image, crop_to_ratio,
    wp_upload_image, build_hotel_buttons_custom, build_tour_buttons,
    _get_top_tour, _section_heading_before, classify_region, wp_get_or_create_category,
    _wp_auth, WP_SITE_URL, COUPANG_LINK, send_telegram,
)
import requests

logger = logging.getLogger("regen")
logging.basicConfig(level=logging.INFO)

# (post_id, destination, topic, continent)
# post_id를 None으로 두면 기존 글을 덮어쓰지 않고 새 글로 발행한다.
TARGETS = [
    (None, "일본", "교통패스(JR패스) 완벽정리", "Asia"),
]


def wp_update_post(post_id, content: dict, media_id, cat_id):
    """post_id가 있으면 그 글을 덮어쓰고, None이면 새 draft 글을 만든다."""
    payload = {
        "title": content["title"],
        "content": content["body"],
        "excerpt": content["excerpt"],
        "slug": content["slug"],
        "meta": {
            "rank_math_focus_keyword": content["focus_kw"],
            "rank_math_description": content["meta_desc"],
        },
    }
    if media_id:
        payload["featured_media"] = media_id
    if cat_id:
        payload["categories"] = [cat_id]
    if post_id is None:
        payload["status"] = "draft"
        url = f"{WP_SITE_URL}/wp-json/wp/v2/posts"
    else:
        url = f"{WP_SITE_URL}/wp-json/wp/v2/posts/{post_id}"
    r = requests.post(url, headers=_wp_auth(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def regenerate_one(post_id, destination: str, topic: str, continent: str):
    logger.info(f"=== {post_id} ({destination} | {topic}) 재생성 시작 ===")
    content = generate_checklist_content(destination, topic, continent)

    hotel_btns = build_hotel_buttons_custom(destination)
    # 투어 버튼은 실제로 삽입되는 섹션의 구체적 주제(예: "JR 패스")를 문맥으로 사용
    tour_context = _section_heading_before(content["body"], "{TOUR_BUTTONS}")
    if not tour_context:
        tour_context = _get_top_tour(destination, content.get("meta_desc", topic))
    tour_btns = build_tour_buttons(destination, tour_context)
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
            content["body"] += html_block

    used_urls: set = set()
    today = datetime.now().strftime("%Y%m%d")
    _KEYWORD_STYLE = "margin-top:6px;font-size:12px;color:#94a3b8;text-align:center;"

    def _keyword_caption(kw: str) -> str:
        return f'<figcaption style="{_KEYWORD_STYLE}">{kw}</figcaption>'

    mid_idx = (len(content["sections"]) + 1) // 2 if content["sections"] else 0
    for idx, section in enumerate(content["sections"], start=1):
        placeholder = f"{{PHOTO:section_{idx}}}"
        if placeholder not in content["body"]:
            continue
        if idx != mid_idx:
            content["body"] = content["body"].replace(placeholder, "")
            continue
        try:
            pair = fetch_travel_image(destination, orientation="landscape",
                                       query=section["query"], section="general", used_urls=used_urls)
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

    media_id = None
    try:
        featured_query = content["sections"][0]["query"] if content["sections"] else destination
        featured_pair = fetch_travel_image(destination, orientation="landscape",
                                            query=featured_query, section="featured", used_urls=used_urls)
        if featured_pair:
            featured_raw, _ = featured_pair
            featured_crop = crop_to_ratio(featured_raw, width=1200, height=675)
            fname = f"{destination.lower().replace(' ', '_')}_featured_{today}.jpg"
            media_result = wp_upload_image(featured_crop, fname, alt=f"{destination} {topic}")
            if media_result:
                media_id = media_result.get("id")
    except Exception as e:
        logger.warning(f"대표 이미지 실패: {e}")

    region = classify_region(destination)
    cat_id = wp_get_or_create_category(region)

    result = wp_update_post(post_id, content, media_id, cat_id)
    logger.info(f"업데이트 완료: {result.get('link')}")
    return result


def main():
    send_telegram(f"임시글 재생성 시작 ({len(TARGETS)}개)")
    results = []
    for post_id, destination, topic, continent in TARGETS:
        try:
            r = regenerate_one(post_id, destination, topic, continent)
            results.append(f"{post_id}: {r.get('link')}")
        except Exception as e:
            logger.error(f"{post_id} 재생성 실패: {e}")
            results.append(f"{post_id}: 실패 ({e})")
        time.sleep(3)
    send_telegram("임시글 재생성 완료\n\n" + "\n".join(results))


if __name__ == "__main__":
    main()
