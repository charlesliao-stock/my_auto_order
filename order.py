import os
import time
import requests
from bs4 import BeautifulSoup
from PIL import Image
import pytesseract
from io import BytesIO

# 從 GitHub Secrets 讀取設定
USERNAME = os.environ.get("KMUH_USERNAME")
PASSWORD = os.environ.get("KMUH_PASSWORD")
MEAL_COUNT = os.environ.get("MEAL_COUNT", "1")  # 預設份數為 1
LINE_NOTIFY_TOKEN = os.environ.get("LINE_NOTIFY_TOKEN")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

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

def solve_captcha(image_bytes):
    """使用 OCR 辨識 4 碼英數字驗證碼"""
    try:
        img = Image.open(BytesIO(image_bytes))
        # 影像預處理以提高辨識率
        img = img.convert('L').point(lambda x: 0 if x < 140 else 255, '1')
        # 設定只辨識英數字共 4 碼
        config = '-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 --psm 8'
        text = pytesseract.image_to_string(img, config=config).strip()
        return text.replace(" ", "")[:4]
    except Exception as e:
        print(f"驗證碼辨識錯誤: {e}")
        return ""

def login_and_order():
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            print(f"--- 開始第 {attempt} 次嘗試訂餐 ---")
            
            # 1. 取得登入頁面與 signin 參數
            auth_home = "https://www.kmuh.org.tw/Web/AuthServerMVC/"
            res = SESSION.get(auth_home)
            soup = BeautifulSoup(res.text, 'html.parser')
            
            form = soup.find('form', {'name': 'form'})
            if not form:
                raise Exception("找不到登入表單")
            
            action_url = form.get('action') # 例如 /Web/AuthServerMVC/login?signin=...
            signin_token = action_url.split('signin=')[1]
            
            xsrf_input = form.find('input', {'name': 'idsrv.xsrf'})
            xsrf_value = xsrf_input.get('value') if xsrf_input else ""

            # 2. 取得驗證碼圖片
            captcha_img_url = f"https://www.kmuh.org.tw/Web/AuthServerMVC/logonworkflow/RenderCaptcha?signin={signin_token}"
            captcha_res = SESSION.get(captcha_img_url)
            
            # 如果回傳的是圖片網址或 base64，這裡視實際情況調整（此處假設直接回傳圖片二進位或 Data URL）
            if captcha_res.content.startswith(b'data:image'):
                # 若是 data URL 格式
                import base64
                img_data = base64.b64decode(captcha_res.text.split(',')[1])
                captcha_code = solve_captcha(img_data)
            else:
                captcha_code = solve_captcha(captcha_res.content)
            
            print(f"辨識出的驗證碼: {captcha_code}")
            if len(captcha_code) != 4:
                print("驗證碼長度不符，重新嘗試...")
                continue

            # 3. 提交登入表單
            login_payload = {
                "idsrv.xsrf": xsrf_value,
                "username": USERNAME,
                "password": PASSWORD,
                "kmuh-captcha": captcha_code
            }
            login_url = f"https://www.kmuh.org.tw{action_url}"
            login_res = SESSION.post(login_url, data=login_payload, allow_redirects=True)
            
            if "登入" in login_res.text and "職編" in login_res.text:
                print("登入失敗（可能驗證碼錯誤或帳密有誤），重試中...")
                continue
            
            print("登入成功！")

            # 4. 透過轉向網址進入訂餐系統
            tran_url = f"https://www.kmuh.org.tw/Web/WebPortal/Home/TranUrl?sysid=583&url=https://www.kmsh.org.tw/web/wwwkmhk/Nutr_Order/pwd.asp&inDBName=ora92"
            SESSION.get(tran_url)

            # 5. 選擇午餐與餐盒類別（健康均衡餐(葷)）
            order_page_url = "https://www.kmsh.org.tw/web/wwwkmhk/Nutr_Order/OrderPers.asp"
            
            # 選擇午餐 (shift_no = 2)
            shift_payload = {
                "br_statusKind": "1",
                "areacode": "H",
                "shift_no": "2"
            }
            res_shift = SESSION.post(order_page_url, data=shift_payload)
            soup_order = BeautifulSoup(res_shift.text, 'html.parser')

            # 6. 選擇日期、份數並填寫分機 6551 後送出
            # （此處會依據網頁跳出的日期選項進行送出，填入分機 6551 與份數）
            # 實際欄位名稱請對應系統的最終送出表單
            final_order_payload = {
                "br_statusKind": "1",
                "areacode": "H",
                "shift_no": "2",
                "ext": "6551",
                "qty": MEAL_COUNT
                # 如有其他日期細節欄位可在此擴充
            }
            
            submit_res = SESSION.post(order_page_url, data=final_order_payload)
            
            if submit_res.status_code == 200:
                msg = f"【高醫員工餐自動訂餐成功】\n餐別：午餐\類別：健康均衡餐(葷)\n份數：{MEAL_COUNT}\n分機：6551"
                print(msg)
                send_notification(msg)
                return True
            else:
                raise Exception(f"送出訂單 HTTP 狀態碼: {submit_res.status_code}")

        except Exception as e:
            print(f"第 {attempt} 次執行發生錯誤: {e}")
            if attempt == max_retries:
                err_msg = f"【高醫員工餐自動訂餐失敗】已達最大重試次數，錯誤原因: {e}"
                send_notification(err_msg)
                raise e
            time.sleep(3)

if __name__ == "__main__":
    login_and_order()
