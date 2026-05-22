import asyncio
import requests
import json
import re
from playwright.async_api import async_playwright

# 1. 본인의 구글 웹 앱(GAS) 배포 URL을 입력하세요.
GAS_WEBAPP_URL = "https://script.google.com/macros/s/AKfycbwRdUyOOc8OL0BSd4LKAqL3B3CanxQ1GGXH5b_xWF-YxZ4Vbm1XT8RgyzYe6Atmr9DP/exec"

async def crawl_softc_time(playwright_page, url):
    """지정한 소프트콘 URL에서 방송 시간을 추출하는 함수"""
    # 주소 끝에 파라미터가 없다면 붙여주고, 이미 있다면 유지합니다.
    target_url = url if "date=" in url else f"{url}?date=thismonth"
    
    try:
        await playwright_page.goto(target_url)
        # 페이지 및 자바스크립트 바인딩 안정화를 위해 4초 대기
        await playwright_page.wait_for_timeout(4000)
        
        # '방송 시간' 이름표 옆에 있는 text-xl 클래스 div 조준
        target_xpath = "//div[contains(text(), '방송 시간')]/following-sibling::div[contains(@class, 'text-xl')]"
        
        # 요소가 나타날 때까지 최대 4초 대기
        await playwright_page.wait_for_selector(f"xpath={target_xpath}", timeout=4000)
        
        # 첫 번째 엘리먼트 값 추출
        broadcast_time = await playwright_page.locator(f"xpath={target_xpath}").first.text_content()

        if broadcast_time is not None:  # 단순히 값이 존재하는지 체크 (문자열 '0'도 통과)
            # 숫자와 마침표(.)만 추출
            clean_time = re.sub(r'[^\d.]', '', broadcast_time)
            
            # clean_time이 빈 문자열이 아니라면 ('0'을 포함한 모든 숫자형 문자열)
            if clean_time != "":
                return float(clean_time)
                
        return 0.0
        
    except Exception as e:
        print(f"   [크롤링 실패] URL: {target_url} | 원인: Timeout 혹은 요소 없음")
        return None

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
            headless=True, # 화면 숨김
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        # 3. 목록을 순회하며 크롤링 진행
        print("2. 소프트콘 방송 시간 크롤링 시작 (백그라운드)")
        for idx, streamer in enumerate(streamer_list):
            s_id = streamer['sId']
            url = streamer['softcUrl']
            
            print(f" [{idx+1}/{len(streamer_list)}] 아이디: {s_id} 크롤링 중...")
            
            result_time = await crawl_softc_time(page, url)
            
            if result_time is not None:
                print(f"   -> 추출 성공: {result_time}")
                payload_to_update.append({
                    "sId": s_id,
                    "broadcastTime": result_time
                })
            
            # 사이트 디도스 방지 및 차단 회피를 위한 1.5초 휴식
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
            # GAS doPost 호출 (자동 리다이렉트 대응을 위해 전송)
            res = requests.post(GAS_WEBAPP_URL, data=json.dumps(post_data), headers={"Content-Type": "application/json"})
            print(f" -> 구글 시트 응답결과: {res.text}")
        except Exception as e:
            print(f"구글 시트 전송 중 오류 발생: {e}")
    else:
        print("\n업데이트할 수집 데이터가 없습니다.")

if __name__ == "__main__":
    asyncio.run(main())
