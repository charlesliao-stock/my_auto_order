import os
import time
from playwright.sync_api import sync_playwright
from PIL import Image
import pytesseract
import requests

USERNAME = os.environ.get("KMUH_USERNAME")
PASSWORD = os.environ.get("KMUH_PASSWORD")
MEAL_COUNT = os.environ.get("MEAL_COUNT", "1")
LINE_NOTIFY_TOKEN = os.environ.get("LINE_NOTIFY_TOKEN")

def send_notification(message):
    """發送 LINE Notify 通知"""
    if not LINE_NOTIFY_TOKEN:
        print(f"[通知] {message}")
        return
    url = "https://notify-api.line.me/api/notify"
    headers = {"Authorization": f"Bearer {LINE_NOTIFY_TOKEN}"}
    data = {"message": message}
    try:
        requests.post(url, headers=headers, data=data)
    except Exception as e:
        print(f"發送 LINE 通知失敗: {e}")

def solve_captcha(image_path):
    """使用 OCR 辨識驗證碼圖片檔案"""
    try:
        img = Image.open(image_path)
        img = img.convert('L').point(lambda x: 0 if x < 140 else 255, '1')
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
                
                # 1. 前往高醫單一入口網，並等待網路完全靜止（確保頁面與驗證碼載入完畢）
                page.goto("https://www.kmuh.org.tw/Web/AuthServerMVC/", wait_until="networkidle")
                page.wait_for_selector("#username", timeout=10000)
                page.screenshot(path=f"screenshots/1_login_page_{attempt}.png")
                
                # 2. 擷取驗證碼圖片並進行 OCR 辨識
                captcha_img = page.locator("#kmuh-captcha-img")
                captcha_img.screenshot(path=f"screenshots/captcha_{attempt}.png")
                
                captcha_code = solve_captcha(f"screenshots/captcha_{attempt}.png")
                print(f"辨識出的驗證碼: {captcha_code}")
                
                if len(captcha_code) != 4:
                    print("驗證碼長度不正確，重新整理重試...")
                    continue

                # 3. 填入帳號、密碼與驗證碼
                page.fill("#username", USERNAME)
                page.fill("#password", PASSWORD)
                page.fill("#kmuh-captcha", captcha_code)
                
                page.screenshot(path=f"screenshots/2_filled_form_{attempt}.png")

                # 4. 點擊登入按鈕
                page.click("#login")
                page.wait_for_timeout(3000)

                # 檢查是否登入失敗（若帳號欄位還在，代表還留在登入頁）
                if page.locator("#username").is_visible():
                    print("登入失敗（可能驗證碼錯誤或帳密有誤），重試中...")
                    page.screenshot(path=f"screenshots/login_failed_{attempt}.png")
                    continue

                print("登入成功！")
                page.screenshot(path=f"screenshots/3_logged_in.png")

                # 5. 透過轉向網址進入訂餐系統
                tran_url = "https://www.kmuh.org.tw/Web/WebPortal/Home/TranUrl?sysid=583&url=https://www.kmsh.org.tw/web/wwwkmhk/Nutr_Order/pwd.asp&inDBName=ora92"
                page.goto(tran_url, wait_until="networkidle")
                page.screenshot(path=f"screenshots/4_order_system_home.png")

                # 6. 選擇午餐 (shift_no = 2)
                page.select_option("select[name='shift_no']", "2")
                page.wait_for_timeout(2000)
                page.screenshot(path=f"screenshots/5_lunch_selected.png")

                # 7. 填寫分機與份數
                try:
                    page.fill("input[name='ext']", "6551")
                    page.fill("input[name='qty']", MEAL_COUNT)
                except Exception:
                    pass
                
                page.screenshot(path=f"screenshots/6_ready_to_submit.png")

                msg = f"【高醫員工餐自動訂餐執行完成】\n已成功通過驗證並送出訂餐頁面。"
                print(msg)
                send_notification(msg)
                browser.close()
                return True

            except Exception as e:
                print(f"第 {attempt} 次執行發生錯誤: {e}")
                page.screenshot(path=f"screenshots/error_attempt_{attempt}.png")
                if attempt == max_retries:
                    err_msg = f"【高醫員工餐自動訂餐失敗】已達最大重試次數，錯誤原因: {e}"
                    send_notification(err_msg)
                    browser.close()
                    raise e
                time.sleep(3)

        browser.close()

if __name__ == "__main__":
    run_automation()
