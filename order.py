import os
import time
from playwright.sync_api import sync_playwright
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract

# 從 GitHub Secrets 讀取設定
USERNAME = os.environ.get("KMUH_USERNAME")
PASSWORD = os.environ.get("KMUH_PASSWORD")
MEAL_COUNT = os.environ.get("MEAL_COUNT", "1")  # 預設份數為 1

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
                page.select_option("select[name='shift_no']", "2")
                page.wait_for_timeout(2000)
                page.screenshot(path=f"screenshots/5_lunch_selected.png")

                # 7. 選擇餐盒類別「健康均衡餐(葷)」 (value="266")
                try:
                    page.check("input[name='classkind'][value='266']")
                    page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"選擇餐盒類別發生錯誤: {e}")

                # 8. 填寫分機資料 (depttel = 6551) 與份數
                # 依據 HTML 結構，日期下拉選單名稱格式如 odrpcs年份日期，此處尋找頁面中可用的訂餐數量下拉選單或直接填寫
                try:
                    page.fill("input[name='depttel']", "6551")
                    
                    # 尋找當前頁面啟用的訂餐數量下拉選單並選擇份數
                    qty_selects = page.locator("select[name^='odrpcs']")
                    if qty_selects.count() > 0:
                        for i in range(qty_selects.count()):
                            sel = qty_selects.nth(i)
                            if not sel.get_attribute("disabled"):
                                sel.select_option(MEAL_COUNT)
                                break
                except Exception as e:
                    print(f"填寫分機或份數時發生錯誤: {e}")

                page.screenshot(path=f"screenshots/6_ready_to_submit.png")

                # 9. 點擊送出按鈕 (B2)
                # 若需要正式送出，可將下方註解取消；測試期間可先保留註解以檢查截圖畫面
                # page.click("input[name='B2']")
                # page.wait_for_timeout(3000)
                # page.screenshot(path=f"screenshots/7_submitted.png")

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
