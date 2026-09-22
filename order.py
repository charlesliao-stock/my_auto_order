import os
import time
from playwright.sync_api import sync_playwright
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract

# 從 GitHub Secrets 讀取設定
USERNAME = os.environ.get("KMUH_USERNAME")
PASSWORD = os.environ.get("KMUH_PASSWORD")
MEAL_COUNT = os.environ.get("MEAL_COUNT", "2")  # 預設份數為 2

def solve_captcha(image_path):
    """使用優化的影像前處理與 OCR 辨識驗證碼"""
    try:
        img = Image.open(image_path)
        
        # 1. 圖片放大 3 倍以提升辨識率
        img = img.resize((img.width * 3, img.height * 3), Image.Resampling.LANCZOS)
        
        # 2. 轉為灰階
        img = img.convert('L')
        
        # 3. 增強對比度
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)
        
        # 4. 二值化處理
        img = img.point(lambda x: 0 if x < 150 else 255, '1')
        
        # 5. 中值濾波降噪
        img = img.filter(ImageFilter.MedianFilter(size=3))
        
        # 6. OCR 辨識設定 (限制大小寫英文與數字 4 碼)
        config = '-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 --psm 8'
        text = pytesseract.image_to_string(img, config=config).strip()
        return text.replace(" ", "")[:4]
    except Exception as e:
        print(f"驗證碼辨識錯誤: {e}")
        return ""

def run_automation():
    os.makedirs("screenshots", exist_ok=True)
    
    with sync_playwright() as p:
        # 啟動背景瀏覽器
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                print(f"--- 開始第 {attempt} 次嘗試訂餐 ---")
                
                # 1. 前往高醫單一入口網
                page.goto("https://www.kmuh.org.tw/Web/Webportal", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_selector("#username", timeout=30000)
                page.screenshot(path=f"screenshots/1_login_page_{attempt}.png")
                
                # 2. 擷取驗證碼圖片並進行 OCR 辨識
                captcha_img = page.locator("#kmuh-captcha-img")
                captcha_img.screenshot(path=f"screenshots/captcha_{attempt}.png")
                
                captcha_code = solve_captcha(f"screenshots/captcha_{attempt}.png")
                print(f"辨識出的驗證碼: {captcha_code}")
                
                if len(captcha_code) != 4:
                    print("驗證碼長度不正確，重新整理重試...")
                    captcha_img.click()
                    page.wait_for_timeout(1000)
                    continue

                # 3. 填入帳號、密碼與驗證碼
                page.fill("#username", USERNAME)
                page.fill("#password", PASSWORD)
                page.fill("#kmuh-captcha", captcha_code)
                
                page.screenshot(path=f"screenshots/2_filled_form_{attempt}.png")

                # 4. 點擊登入按鈕
                page.click("#login")
                page.wait_for_timeout(4000)

                # 檢查是否登入失敗
                if page.locator("#username").is_visible():
                    print("登入失敗（可能驗證碼錯誤或帳密有誤），重試中...")
                    page.screenshot(path=f"screenshots/login_failed_{attempt}.png")
                    continue

                print("登入成功！")
                page.screenshot(path=f"screenshots/3_logged_in.png")

                # 5. 透過轉向網址進入營養部訂餐系統
                tran_url = "https://www.kmuh.org.tw/Web/WebPortal/Home/TranUrl?sysid=583&url=https://www.kmsh.org.tw/web/wwwkmhk/Nutr_Order/pwd.asp&inDBName=ora92"
                page.goto(tran_url, wait_until="domcontentloaded", timeout=30000)
                page.screenshot(path=f"screenshots/4_order_system_home.png")

                # 6. 餐別選擇午餐 (shift_no = 2)
                # 注意：實際頁面中 shift_no 的 <select> 有 OnChange="form11.submit()"，
                # 選擇後會觸發整頁重新載入（非單純 AJAX 局部更新），因此改用等待導覽完成，
                # 比固定 timeout 更可靠；若網路較慢，wait_for_timeout 可能還沒抓到新頁面內容。
                with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                    page.select_option("select[name='shift_no']", "2")
                page.screenshot(path=f"screenshots/5_lunch_selected.png")

                # 7. 選擇餐盒類別「健康均衡餐(葷)」 (value="266")
                # 注意：實際頁面中這是 radio (OnClick="form12.submit()")，且「健康均衡餐(葷)」
                # 目前為預設已勾選項目。Playwright 的 check() 若偵測到已勾選則不會觸發 click，
                # 也就不會重新整理頁面；只有在需要「切換」選項時才會真的送出 form12 並整頁重載，
                # 這裡先判斷是否已勾選，只有真的要改變時才等待導覽完成。
                try:
                    classkind_266 = page.locator("input[name='classkind'][value='266']")
                    if classkind_266.is_checked():
                        print("餐盒類別「健康均衡餐(葷)」已是預設選項，無需切換")
                    else:
                        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                            classkind_266.check()
                except Exception as e:
                    print(f"選擇餐盒類別發生錯誤: {e}")
                page.wait_for_timeout(1000)

                # 8. 填寫分機資料 (depttel = 6551) 與份數
                # 依實際頁面結構確認：每個日期各自有一個 <select name="odrpcs民國年月日">
                # (例如 odrpcs1150923，即民國115年9月23日)。「中(N)」顯示的是該日期午餐目前
                # 「剩餘可訂份數」，N=0 時該 <select> 會被 disabled（代表已訂完，不是「尚未開放」）；
                # 只要還有剩餘份數，該日期就是可下拉選擇的，因此同一週可能同時有多天都是開放的。
                # 下拉選單的選項也是依剩餘份數動態產生（例如剩 1 份，選單就只會有 "--"、"1"，不會有 "2"）。
                # 這裡的邏輯：在所有未 disabled（尚有剩餘份數）的日期中，優先挑「日期最新」且「剩餘份數
                # 足夠 MEAL_COUNT」的一筆；若最新那天份數不夠，依日期新舊往前找，取第一筆份數足夠的；
                # 若全部都不夠，才退而求其次選「最新一筆、但只能給的剩餘份數」，並明確印出警告。
                try:
                    page.fill("input[name='depttel']", "6551")

                    qty_selects = page.locator("select[name^='odrpcs']")
                    count = qty_selects.count()

                    candidates = []  # (日期數字, select locator, 該日期可選的份數清單)
                    for i in range(count):
                        sel = qty_selects.nth(i)
                        if sel.get_attribute("disabled") is not None:
                            continue  # 已無剩餘份數（訂完），跳過
                        name = sel.get_attribute("name") or ""
                        digits = "".join(ch for ch in name if ch.isdigit())
                        if not digits:
                            continue
                        option_values = [
                            v for v in sel.locator("option").evaluate_all(
                                "els => els.map(e => e.value)"
                            )
                            if v.strip().isdigit()
                        ]
                        candidates.append((int(digits), sel, option_values))

                    order_date_selected = False
                    if candidates:
                        # 依日期數字由新到舊排序
                        candidates.sort(key=lambda x: x[0], reverse=True)

                        chosen = None
                        for date_digits, sel, option_values in candidates:
                            if MEAL_COUNT in option_values:
                                chosen = (sel, MEAL_COUNT, date_digits)
                                break

                        if chosen is None:
                            # 沒有任何一天的剩餘份數足夠 MEAL_COUNT，退而求其次：
                            # 用「日期最新」那筆，選它剩餘份數清單中的最大值
                            latest_digits, latest_sel, latest_options = candidates[0]
                            if latest_options:
                                fallback_count = str(max(int(v) for v in latest_options))
                                chosen = (latest_sel, fallback_count, latest_digits)
                                print(
                                    f"警告：最新可訂日期剩餘份數不足 {MEAL_COUNT} 份，"
                                    f"改為訂購剩餘可提供的 {fallback_count} 份，請務必確認截圖與實際需求是否相符"
                                )

                        if chosen:
                            target_sel, target_count, target_digits = chosen
                            target_sel.select_option(target_count)
                            order_date_selected = True
                            print(f"已選擇日期(數字){target_digits}的訂餐數量選單 -> {target_count} 份")
                        else:
                            print("警告：找到開放中的日期，但其剩餘份數選單為空，無法下單")
                    else:
                        # 目前沒有任何一筆日期還有剩餘份數（全部已訂完/disabled）
                        print("警告：目前所有日期的午餐皆已訂完（剩餘份數為 0），本次無法訂餐")
                except Exception as e:
                    order_date_selected = False
                    print(f"填寫分機或份數時發生錯誤: {e}")

                page.screenshot(path=f"screenshots/6_ready_to_submit.png")

                # 9. 點擊送出按鈕 (B2)
                # B2 送出按鈕實際上是在同一頁的 form3 (action="OrderPers_ins.asp") 裡面。
                # 若需要正式送出，可將下方註解取消；測試期間可先保留註解以檢查截圖畫面。
                # 加上 order_date_selected 判斷，避免在沒有任何可訂日期時誤送出空白訂單。
                # if order_date_selected:
                #     page.click("input[name='B2']")
                #     page.wait_for_timeout(3000)
                #     page.screenshot(path=f"screenshots/7_submitted.png")
                # else:
                #     print("因無可訂購日期，本次不送出訂單")

                print("【高醫員工餐自動訂餐流程執行完成】")
                browser.close()
                return True

            except Exception as e:
                print(f"第 {attempt} 次執行發生錯誤: {e}")
                page.screenshot(path=f"screenshots/error_attempt_{attempt}.png")
                if attempt == max_retries:
                    print(f"【高醫員工餐自動訂餐失敗】已達最大重試次數，錯誤原因: {e}")
                    browser.close()
                    raise e
                time.sleep(3)

        browser.close()

if __name__ == "__main__":
    run_automation()
