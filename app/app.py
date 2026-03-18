import os
import re
import requests
import anthropic
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=5)

NOTION_TOKEN = os.environ["NOTION_INTERNAL_INTEGRATION_SECRET"]
NOTION_DB_ID = os.environ["NOTION_DATABASE_ID"]
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}


def notion_get(source_id):
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    payload = {"filter": {"property": "source_id", "title": {"equals": source_id}}}
    res = requests.post(url, headers=NOTION_HEADERS, json=payload)
    results = res.json().get("results", [])
    if results:
        props = results[0]["properties"]
        lang1_list = props["lang1"]["rich_text"]
        lang2_list = props["lang2"]["rich_text"]
        return {
            "lang1": lang1_list[0]["plain_text"] if lang1_list else "",
            "lang2": lang2_list[0]["plain_text"] if lang2_list else "",
            "page_id": results[0]["id"],
        }
    return None


def notion_set(source_id, lang1, lang2):
    properties = {
        "source_id": {"title": [{"text": {"content": source_id}}]},
        "lang1": {"rich_text": [{"text": {"content": lang1}}]},
        "lang2": {"rich_text": [{"text": {"content": lang2}}]},
    }
    existing = notion_get(source_id)
    if existing:
        requests.patch(
            f"https://api.notion.com/v1/pages/{existing['page_id']}",
            headers=NOTION_HEADERS,
            json={"properties": properties},
        )
    else:
        requests.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": NOTION_DB_ID}, "properties": properties},
        )


def notion_delete(source_id):
    existing = notion_get(source_id)
    if existing:
        requests.patch(
            f"https://api.notion.com/v1/pages/{existing['page_id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
        return True
    return False


def get_source_id(event):
    source = event.source
    if source.type == "group":
        return f"group_{source.group_id}"
    elif source.type == "room":
        return f"room_{source.room_id}"
    return f"user_{source.user_id}"


def translate(text, lang1, lang2):
    def resolve_chinese(lang):
        simplified_keywords = ("簡體", "简体", "Simplified", "簡中", "简中")
        if any(k in lang for k in simplified_keywords):
            return "Simplified Chinese (簡體中文)"
        if lang in ("中文", "Chinese", "CH", "ch", "ZH", "zh", "繁體", "繁體中文", "繁中",
                    "Traditional Chinese", "Mandarin", "mandarin") or \
           "中文" in lang:
            return "Traditional Chinese (繁體中文)"
        return lang

    resolved_lang1 = resolve_chinese(lang1)
    resolved_lang2 = resolve_chinese(lang2)

    prompt = (
        f"You are a bilingual translator between {resolved_lang1} and {resolved_lang2}.\n"
        f"Detect the language of the following text.\n"
        f"If it is {resolved_lang1}, translate it into {resolved_lang2}.\n"
        f"If it is {resolved_lang2}, translate it into {resolved_lang1}.\n"
        f"If the text is not in either language, reply exactly with: "
        f"⚠️ 無法識別語言，請確認設定的語言是否正確。\n"
        f"Output only the translation, no explanations.\n\n"
        f"Text:\n{text}"
    )
    message = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    source_id = get_source_id(event)

    # /setlang <語言1> <語言2>
    if text.lower().startswith("/setlang "):
        arg = text[len("/setlang "):].strip()
        # 支援空白、全形逗號、半形逗號作為分隔符
        parts = re.split(r"[，,\s]+", arg, maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            lang1, lang2 = parts[0].strip(), parts[1].strip()
            notion_set(source_id, lang1, lang2)
            reply = (
                f"✅ 翻譯語言設定成功！\n"
                f"🔤 語言1：{lang1}\n"
                f"🔤 語言2：{lang2}\n\n"
                f"說 {lang1} 會自動翻成 {lang2}，說 {lang2} 會自動翻成 {lang1}。"
            )
        else:
            reply = (
                "❌ 格式錯誤\n"
                "使用方式：/setlang <語言1> <語言2>\n"
                "範例：/setlang 中文 英文"
            )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # /status — 查看目前設定
    if text.lower() == "/status":
        s = notion_get(source_id)
        if s:
            reply = (
                f"📊 目前翻譯設定\n"
                f"🔤 語言1：{s['lang1']}\n"
                f"🔤 語言2：{s['lang2']}"
            )
        else:
            reply = "⚠️ 尚未設定翻譯語言\n請使用 /setlang 指令設定"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # /stop — 停止翻譯
    if text.lower() == "/stop":
        if notion_delete(source_id):
            reply = "🛑 已停止翻譯"
        else:
            reply = "⚠️ 翻譯尚未啟動"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # /help — 顯示說明
    if text.lower() in ("/help", "/說明"):
        reply = (
            "📖 LINE 翻譯機器人\n\n"
            "指令列表：\n"
            "/setlang <語言1> <語言2>\n"
            "  設定翻譯語言\n"
            "  範例：/setlang 中文 英文\n\n"
            "/status  查看目前設定\n\n"
            "/stop    停止翻譯\n\n"
            "/help    顯示此說明\n\n"
            "語言名稱範例：\n"
            "中文、英文、日文、韓文、法文、德文、西班牙文"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 一般訊息 — 如果有設定語言則自動翻譯
    s = notion_get(source_id)
    if s is None:
        reply = (
            "⚠️ 請設置語言\n\n"
            "📖 LINE 翻譯機器人\n\n"
            "指令列表：\n"
            "/setlang <語言1> <語言2>\n"
            "  設定翻譯語言\n"
            "  範例：/setlang 中文 英文\n\n"
            "/status  查看目前設定\n\n"
            "/stop    停止翻譯\n\n"
            "/help    顯示此說明\n\n"
            "語言名稱範例：\n"
            "中文、英文、日文、韓文、法文、德文、西班牙文"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if s:
        try:
            result = translate(text, s["lang1"], s["lang2"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                msg = "❌ 翻譯服務暫時無法使用，請稍後再試"
            else:
                msg = f"❌ 翻譯失敗：{e.message}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        except Exception as e:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"❌ 翻譯失敗：{e}"),
            )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
