import asyncio
import requests
import json
import re
import os
import sys
from playwright.async_api import async_playwright

# 깃허브 액션 연동을 고려하여 주소를 가져옵니다. (로컬 실행 시 주소를 직접 적으셔도 됩니다)
GAS_WEBAPP_URL = os.environ.get("GAS_URL", "https://script.google.com/macros/s/AKfycbwyBnQwA2lnzCLFt6_vbv7397ClSPOjtFlH53p1qJ2ziJjA8y8KQ9NJ-UF8VkDgNeeRxQ/exec")

if not GAS_WEBAPP_URL:
    print("오류: 구글 웹 앱 URL(GAS_URL)이 세팅되지 않았습니다.")
    sys.exit(1)

async def crawl_softc_data(playwright_page, url):
    """지정한 소프트콘 URL에서 방송 시간과 평균 시청자를 동시에 추출하는 함수"""
    target_url = url if "date=" in url else f"{url}?date=thismonth"
    
    # 기본값 설정
    result = {"time": 0.0, "viewers": 0.0}
    
    try:
        await playwright_page.goto(target_url)
        await playwright_page.wait_for_timeout(4000) # 페이지 안정화 대기
        
        # 1. 방송 시간 추출 및 정제
        time_xpath = "//div[contains(text(), '방송 시간')]/following-sibling::div[contains(@class, 'text-xl')]"
        try:
            await playwright_page.wait_for_selector(f"xpath={time_xpath}", timeout=3000)
            time_raw = await playwright_page.locator(f"xpath={time_xpath}").first.text_content()
            if time_raw:
                clean_time = re.sub(r'[^0-9.]', '', time_raw)
                if clean_time.strip() != "":
                    result["time"] = float(clean_time)
        except Exception:
            pass # 요소를 못 찾으면 기본값 0.0 유지

        # 2. 평균 시청자 추출 및 정제 ("평균 시청자" 이름표 옆의 text-xl 탐색)
        viewers_xpath = "//div[contains(text(), '평균 시청자')]/following-sibling::div[contains(@class, 'text-xl')]"
        try:
            await playwright_page.wait_for_selector(f"xpath={viewers_xpath}", timeout=3000)
            viewers_raw = await playwright_page.locator(f"xpath={viewers_xpath}").first.text_content()
            if viewers_raw:
                # 콤마(,), 따옴표 등을 제거하고 순수 숫자와 마침표만 추출
                clean_viewers = re.sub(r'[^0-9.]', '', viewers_raw)
                if clean_viewers.strip() != "":
                    result["viewers"] = float(clean_viewers)
        except Exception:
            pass # 요소를 못 찾으면 기본값 0.0 유지
            
        return result
        
    except Exception as e:
        print(f"   [크롤링 실패/제한] URL: {target_url} | 기본값(0.0) 처리")
        return result

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
    payload_to_update = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        # 3. 목록을 순회하며 크롤링 진행
        print("2. 소프트콘 방송 데이터 크롤링 시작 (백그라운드)")
        for idx, streamer in enumerate(streamer_list):
            s_id = streamer['sId']
            url = streamer['softcUrl']
            
            print(f" [{idx+1}/{len(streamer_list)}] 아이디: {s_id} 크롤링 중...")
            
            # 시간과 평청자 데이터를 동시에 딕셔너리로 받아옴
            data_res = await crawl_softc_data(page, url)
            
            print(f"   -> 추출 성공 | 방송시간: {data_res['time']} | 평균시청자: {data_res['viewers']}")
            
            payload_to_update.append({
                "sId": s_id,
                "broadcastTime": data_res["time"],
                "avgViewers": data_res["viewers"] # 새롭게 추가된 데이터 필드
            })
            
            # 차단 방지를 위한 휴식
            await page.wait_for_timeout(1500)
            
        await browser.close()

    # 4. 수집된 결과를 구글 시트(GAS)로 전송하여 벌크 업데이트
    if payload_to_update:
        print(f"\n3. 크롤링 완료된 {len(payload_to_update)}건의 데이터를 구글 시트에 전송 중...")
        post_data = {
            "action": "updateSoftcTime",
            "payload": payload_to_update
        }
        
        try:
            res = requests.post(GAS_WEBAPP_URL, data=json.dumps(post_data), headers={"Content-Type": "application/json"})
            print(f" -> 구글 시트 응답결과: {res.text}")
        except Exception as e:
            print(f"구글 시트 전송 중 오류 발생: {e}")
    else:
        print("\n업데이트할 수집 데이터가 없습니다.")

if __name__ == "__main__":
    asyncio.run(main())
