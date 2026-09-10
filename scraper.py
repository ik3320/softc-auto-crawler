import asyncio
import requests
import json
import re
import os
import sys
import random
from playwright.async_api import async_playwright

# 깃허브 액션 연동을 고려하여 주소를 가져옵니다. (로컬 실행 시 주소를 직접 적으셔도 됩니다)
GAS_WEBAPP_URL = os.environ.get("GAS_WEB_APP_URL")

if not GAS_WEBAPP_URL:
    print("오류: 구글 웹 앱 URL(GAS_URL)이 세팅되지 않았습니다.")
    sys.exit(1)

# 재시도/딜레이 설정
RETRY_MAX = 3                      # 진짜 실패(None) 시 최대 시도 횟수
RETRY_DELAY_MS = 15000             # 재시도 전 대기 (15초)
DELAY_MIN_MS = 6000                # 스트리머 간 최소 대기
DELAY_MAX_MS = 10000               # 스트리머 간 최대 대기
CONTEXT_RESET_EVERY = 20           # 몇 건마다 컨텍스트 재생성
CONTEXT_RESET_COOLDOWN_MS = 30000  # 컨텍스트 재생성 후 쿨다운


async def crawl_softc_data(playwright_page, url):
    """
    지정한 소프트콘 URL에서 방송 시간과 평균 시청자를 동시에 추출.

    반환값:
      - ok=True  : 페이지가 정상 로드되어 값을 읽어냄 (0.0도 정상값 = 미방송)
      - ok=False : 페이지 로드 실패 / 차단 / 요소 자체가 없음 (진짜 실패)
    """
    target_url = url if "date=" in url else f"{url}?date=thismonth"

    # None = 아직 못 읽음, ok = 값 추출 성공 여부
    result = {"time": None, "viewers": None, "ok": False}

    try:
        resp = await playwright_page.goto(target_url, timeout=15000)
        await playwright_page.wait_for_timeout(4000)

        # 진단: HTTP 상태코드
        if resp and resp.status >= 400:
            print(f"   [HTTP {resp.status}] {target_url}")

        # 페이지 텍스트 확보
        try:
            title = await playwright_page.title()
        except Exception:
            title = "(title 조회 실패)"

        try:
            body_text = await playwright_page.locator("body").inner_text(timeout=3000)
        except Exception:
            body_text = ""

        has_time_label = "방송 시간" in body_text
        has_viewers_label = "평균 시청자" in body_text

        # 페이지 이상 감지 (라벨 자체가 없으면 차단/에러 페이지)
        if not has_time_label and not has_viewers_label:
            preview = body_text[:150].replace("\n", " ")
            print(f"   [페이지 이상] title='{title}' / body앞부분='{preview}'")

        # 1. 방송 시간 추출
        time_xpath = "//div[contains(text(), '방송 시간')]/following-sibling::div[contains(@class, 'text-xl')]"
        try:
            await playwright_page.wait_for_selector(f"xpath={time_xpath}", timeout=3000)
            time_raw = await playwright_page.locator(f"xpath={time_xpath}").first.text_content()
            if time_raw:
                clean_time = re.sub(r'[^0-9.]', '', time_raw)
                if clean_time.strip() != "":
                    result["time"] = float(clean_time)
        except Exception:
            pass

        # 2. 평균 시청자 추출
        viewers_xpath = "//div[contains(text(), '평균 시청자')]/following-sibling::div[contains(@class, 'text-xl')]"
        try:
            await playwright_page.wait_for_selector(f"xpath={viewers_xpath}", timeout=3000)
            viewers_raw = await playwright_page.locator(f"xpath={viewers_xpath}").first.text_content()
            if viewers_raw:
                clean_viewers = re.sub(r'[^0-9.]', '', viewers_raw)
                if clean_viewers.strip() != "":
                    result["viewers"] = float(clean_viewers)
        except Exception:
            pass

        # ★ 라벨은 렌더링됐는데 파싱 실패한 경우 → 0.0으로 채움 (미방송 상태)
        if has_time_label and result["time"] is None:
            result["time"] = 0.0
        if has_viewers_label and result["viewers"] is None:
            result["viewers"] = 0.0

        # ★ ok 판정: 페이지 라벨이 하나라도 존재하면 "정상 페이지"
        # 0.0도 유효한 값(=미방송)이므로 ok=True 처리
        if has_time_label or has_viewers_label:
            result["ok"] = True
            if result["time"] is None:
                result["time"] = 0.0
            if result["viewers"] is None:
                result["viewers"] = 0.0

        return result

    except Exception as e:
        print(f"   [예외] {type(e).__name__}: {e}")
        return result


async def crawl_with_retry(page, url, s_name):
    """
    페이지가 정상 로드되어 ok=True가 나오면 성공.
    0.0이라도 ok=True면 재시도하지 않음 (실제 미방송 상태이므로).
    ok=False(진짜 실패)인 경우에만 재시도.
    """
    last_res = None
    for attempt in range(1, RETRY_MAX + 1):
        res = await crawl_softc_data(page, url)
        last_res = res

        # ★ 성공 조건: ok == True (0.0이어도 정상값으로 간주)
        if res["ok"]:
            if (res["time"] or 0) == 0 and (res["viewers"] or 0) == 0:
                print(f"   -> 방송 기록 없음 (0시간 / 0명)")
            return res

        # ok가 False인 경우에만 재시도
        if attempt < RETRY_MAX:
            print(f"   [{s_name}] 재시도 {attempt}/{RETRY_MAX - 1} (15초 대기)...")
            await page.wait_for_timeout(RETRY_DELAY_MS)

    return last_res


async def main():
    # 1. GAS로부터 대상 스트리머 목록 받아오기
    print("1. 구글 시트에서 소프트콘 수집 대상 목록을 불러오는 중...")
    try:
        response = requests.get(f"{GAS_WEBAPP_URL}?action=getSoftcList")
        streamer_list = response.json()
        print(f" -> 총 {len(streamer_list)}명의 대상 스트리머를 확인했습니다.\n")
    except Exception as e:
        print(f"GAS 데이터 로드 실패: {e}")
        return

    if not streamer_list:
        print("수집할 대상이 없습니다. 소프트콘주소 열을 확인하세요.")
        return

    # 2. Playwright 백그라운드 브라우저 시동
    payload_to_update = []   # 시트로 전송할 데이터 (미방송 0 포함)
    skipped = []             # 진짜 실패 (페이지 로드 X)
    zero_list = []           # 미방송 (0/0)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )

        async def new_context_page():
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080}
            )
            pg = await ctx.new_page()
            return ctx, pg

        context, page = await new_context_page()

        # 3. 목록을 순회하며 크롤링 진행
        print("2. 소프트콘 방송 데이터 크롤링 시작 (백그라운드)")
        for idx, streamer in enumerate(streamer_list):
            s_id = streamer.get('sId', '').strip()
            s_name = streamer.get('sName', '').strip() or s_id
            url = streamer.get('softcUrl', '').strip()

            print(f" [{idx+1}/{len(streamer_list)}] {s_name} (아이디: {s_id}) 크롤링 중...")

            data_res = await crawl_with_retry(page, url, s_name)

            if data_res["ok"]:
                # ★ 미방송(0/0)도 시트에 반영 → "이번 달 방송 안 함" 정보 보존
                is_zero = (data_res["time"] or 0) == 0 and (data_res["viewers"] or 0) == 0
                if is_zero:
                    print(f"   -> 추출 성공 | 방송시간: 0.0 | 평균시청자: 0.0 (미방송, 시트 반영)")
                    zero_list.append(s_name)
                else:
                    print(f"   -> 추출 성공 | 방송시간: {data_res['time']} | 평균시청자: {data_res['viewers']}")

                payload_to_update.append({
                    "sId": s_id,
                    "sName": s_name,
                    "broadcastTime": data_res["time"],
                    "avgViewers": data_res["viewers"]
                })
            else:
                # 페이지 자체 로드 실패 / 완전 차단 → 시트 반영 안 함 (기존 값 유지)
                print(f"   -> [스킵] {s_name} - 페이지 로드 실패, 시트 반영 안 함")
                skipped.append(s_name)

            # 차단 방지를 위한 랜덤 휴식
            await page.wait_for_timeout(random.randint(DELAY_MIN_MS, DELAY_MAX_MS))

            # 주기적으로 컨텍스트 재생성 + 쿨다운 + 웜업
            if (idx + 1) % CONTEXT_RESET_EVERY == 0 and (idx + 1) < len(streamer_list):
                print(f"   [컨텍스트 재생성] {CONTEXT_RESET_COOLDOWN_MS // 1000}초 쿨다운...")
                try:
                    await page.close()
                    await context.close()
                except Exception:
                    pass
                context, page = await new_context_page()

                # ★ 웜업: 새 세션의 첫 요청 429 완화
                try:
                    await page.goto("https://viewership.softc.one/", timeout=15000)
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass

                await page.wait_for_timeout(CONTEXT_RESET_COOLDOWN_MS)

        await browser.close()

    # 4. 결과 요약
    print(f"\n3. 결과 요약")
    print(f"   - 방송 있음     : {len(payload_to_update) - len(zero_list)}건")
    print(f"   - 미방송(0/0)   : {len(zero_list)}건")
    print(f"   - 스킵(로드실패): {len(skipped)}건")

    if zero_list:
        print(f"   미방송 스트리머: {', '.join(zero_list)}")
    if skipped:
        print(f"   스킵된 스트리머: {', '.join(skipped)}")

    # 5. 수집된 결과를 구글 시트(GAS)로 전송하여 벌크 업데이트
    if payload_to_update:
        print(f"\n4. 크롤링 완료된 {len(payload_to_update)}건의 데이터를 구글 시트에 전송 중...")
        post_data = {
            "action": "updateSoftcTime",
            "payload": payload_to_update
        }

        try:
            res = requests.post(
                GAS_WEBAPP_URL,
                data=json.dumps(post_data),
                headers={"Content-Type": "application/json"}
            )
            print(f" -> 구글 시트 응답결과: {res.text}")
        except Exception as e:
            print(f"구글 시트 전송 중 오류 발생: {e}")
    else:
        print("\n업데이트할 수집 데이터가 없습니다.")


if __name__ == "__main__":
    asyncio.run(main())
