import os
import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, HTTPException
from qstash import QStash
from upstash_redis import Redis

app = FastAPI()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
OPERATION_START = time(*map(int, os.getenv("OPERATION_START", "09:00").split(":")))
OPERATION_END = time(*map(int, os.getenv("OPERATION_END", "18:00").split(":")))
TZ = ZoneInfo(os.getenv("OPERATION_TIMEZONE", "Asia/Seoul"))
BASE_URL = os.getenv("BASE_URL", "")
QSTASH_TOKEN = os.getenv("QSTASH_TOKEN", "")

# 채널톡 멤버ID → 이름 매핑
MEMBER_NAME_MAP = {
    "491085": "고구망",
    "535653": "인절미",
    "598956": "나나",
}

qstash = QStash(token=QSTASH_TOKEN)
redis = Redis.from_env()


def is_operation_hours() -> bool:
    now_dt = datetime.now(TZ)
    if now_dt.weekday() >= 5:  # 5=토요일, 6=일요일
        return False
    return OPERATION_START <= now_dt.time() <= OPERATION_END


async def send_slack(message: str):
    async with httpx.AsyncClient() as client:
        await client.post(SLACK_WEBHOOK_URL, json={"text": message})


def cancel_existing_timer(chat_id: str):
    msg_id = redis.get(f"timer:{chat_id}")
    if msg_id:
        try:
            qstash.message.cancel(msg_id)
        except Exception:
            pass
        redis.delete(f"timer:{chat_id}")


def schedule_timer(chat_id: str, delay_seconds: int, alert_payload: dict):
    cancel_existing_timer(chat_id)
    res = qstash.message.publish_json(
        url=f"{BASE_URL}/alert",
        body=alert_payload,
        delay=delay_seconds,
    )
    redis.set(f"timer:{chat_id}", res.message_id, ex=delay_seconds + 60)


@app.post("/webhook/channel")
async def channel_webhook(request: Request):
    payload = await request.json()
    event_type = payload.get("event", {}).get("type", "")

    if event_type == "chat_message_created":
        sender_type = payload.get("entity", {}).get("personType", "")
        if sender_type == "user":
            await handle_customer_message(payload)
        elif sender_type == "member":
            chat_id = payload.get("chat", {}).get("id", "")
            if chat_id:
                cancel_existing_timer(chat_id)

    elif event_type == "chat_assigned":
        chat_id = payload.get("chat", {}).get("id", "")
        if chat_id:
            cancel_existing_timer(chat_id)

    return {"ok": True}


async def handle_customer_message(payload: dict):
    if not is_operation_hours():
        return

    chat = payload.get("chat", {})
    message = payload.get("entity", {})

    chat_id = chat.get("id", "unknown")
    chat_title = chat.get("name") or chat_id
    customer_name = payload.get("user", {}).get("name", "고객")
    msg_preview = (message.get("plainText", "") or "")[:50]

    assignee = chat.get("assignee")

    if assignee is None:
        alert_payload = {
            "type": "unassigned",
            "chat_id": chat_id,
            "chat_title": chat_title,
            "customer_name": customer_name,
            "msg_preview": msg_preview,
            "assignee_name": "미배정",
        }
        schedule_timer(chat_id, delay_seconds=5 * 60, alert_payload=alert_payload)

    else:
        assignee_id = str(assignee.get("id", ""))
        assignee_name = MEMBER_NAME_MAP.get(assignee_id, "알 수 없음")
        alert_payload = {
            "type": "assigned",
            "chat_id": chat_id,
            "chat_title": chat_title,
            "customer_name": customer_name,
            "msg_preview": msg_preview,
            "assignee_name": assignee_name,
        }
        schedule_timer(chat_id, delay_seconds=5 * 60, alert_payload=alert_payload)


@app.post("/alert")
async def receive_alert(request: Request):
    body = await request.body()
    signature = request.headers.get("upstash-signature", "")

    try:
        qstash.receiver.verify(
            body=body.decode(),
            signature=signature,
            url=f"{BASE_URL}/alert",
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(body)
    alert_type = data.get("type")
    chat_title = data.get("chat_title", "알 수 없음")
    customer_name = data.get("customer_name", "고객")
    msg_preview = data.get("msg_preview", "")
    assignee_name = data.get("assignee_name", "")

    if alert_type == "unassigned":
        msg = (
            f"🔴 *미배정 문의 미응답 알림*\n"
            f"> 채팅방: {chat_title}\n"
            f"> 고객: {customer_name}\n"
            f"> 마지막 메시지: {msg_preview}\n"
            f"> ⏰ 5분째 미응답 중입니다!"
        )
    elif alert_type == "assigned":
        msg = (
            f"🟡 *문의 미응답 알림*\n"
            f"> 담당자: {assignee_name}\n"
            f"> 채팅방: {chat_title}\n"
            f"> 고객: {customer_name}\n"
            f"> 마지막 메시지: {msg_preview}\n"
            f"> ⏰ 5분째 미응답 중입니다!"
        )
    else:
        return {"ok": True}

    await send_slack(msg)
    return {"ok": True}
