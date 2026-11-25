# ===== วางโค้ดนี้ในกล่อง Python+ ของ CiRA CORE =====
# flow:  Button Run -> WebCam/VideoFile -> DeepDetect -> Python+ (กล่องนี้) -> Debug
# ทำหน้าที่: ส่งจำนวนสินค้าแต่ละชนิดที่ DeepDetect ตรวจเจอ ไปให้เซิร์ฟเวอร์ Smart Counter
#
# ถ้ารันเซิร์ฟเวอร์คนละเครื่องกับ CiRA CORE ให้แก้ SERVER เป็น IP ของเครื่องเซิร์ฟเวอร์
# เช่น "http://192.168.1.50:5000/api/detect"

import time
import requests

SERVER = "http://127.0.0.1:5000/api/detect"
SEND_EVERY = 0.5          # ส่งอย่างมากทุก 0.5 วินาที (ไม่ต้องส่งทุกเฟรม)

last_send = 0
session = requests.Session()


def handle_request(req):
    global last_send
    payload, img = getReqData(req)

    now = time.time()
    if now - last_send >= SEND_EVERY:
        last_send = now

        # obj_count = สรุปจำนวนต่อชนิด เช่น [{'name': 'AuraWater0-5ml', 'count': 2}, ...]
        items = {}
        for o in payload.get('DeepDetect', {}).get('obj_count', []):
            items[o['name']] = o['count']

        try:
            session.post(SERVER, json={"items": items}, timeout=0.3)
        except Exception as e:
            print('send failed:', e)

    # ส่ง payload และภาพต่อไปให้กล่องถัดไปตามเดิม
    resp = makeRespData(payload, img)
    return resp
