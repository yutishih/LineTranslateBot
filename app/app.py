import os
import re
import requests
import anthropic
import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=5)

NOTION_TOKEN = os.environ["NOTION_INTERNAL_INTEGRATION_SECRET"]

# 新版 Notion Data Source 架構
NOTION_DATA_SOURCE_ID = os.environ["NOTION_DATA_SOURCE_ID"]
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2025-09-03",
}


def notion_get(source_id):
    # 2025-09-03: 改用 data source 查詢
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    payload = {
        "filter": {
            "property": "source_id",
            "rich_text": {"equals": source_id}
        }
    }
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
        "source_id": {"rich_text": [{"text": {"content": source_id}}]},
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
        # 嘗試建立新 page，加入簡單重試與衝突檢查（避免序號重複）
        url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
        max_attempts = 5
        attempt = 0
        created_page_id = None
        next_serial = 1
        while attempt < max_attempts:
            attempt += 1
            # 取得目前最大 sysSerial
            try:
                payload = {"sorts": [{"property": "sysSerial", "direction": "descending"}], "page_size": 1}
                r = requests.post(url, headers=NOTION_HEADERS, json=payload)
                results = r.json().get("results", [])
                serial_type = None
                if results:
                    p = results[0].get("properties", {})
                    s_prop = p.get("sysSerial", {})
                    serial_val = None
                    if "title" in s_prop and s_prop.get("title"):
                        serial_val = s_prop["title"][0].get("plain_text") or s_prop["title"][0].get("text", {}).get("content")
                        serial_type = "title"
                    elif "rich_text" in s_prop and s_prop.get("rich_text"):
                        serial_val = s_prop["rich_text"][0].get("plain_text")
                        serial_type = "rich_text"
                    elif "number" in s_prop and s_prop.get("number") is not None:
                        serial_val = str(s_prop.get("number"))
                        serial_type = "number"
                    try:
                        next_serial = int(serial_val) + 1 if serial_val is not None else 1
                    except Exception:
                        next_serial = 1
                else:
                    next_serial = 1
                    serial_type = "number"
            except Exception:
                next_serial = 1

            # 建立頁面（包含 date 與 sysSerial）；根據偵測到的 serial_type 選擇正確格式
            date_iso = datetime.date.today().isoformat()
            if serial_type == "number":
                properties["sysSerial"] = {"number": next_serial}
            elif serial_type == "rich_text":
                properties["sysSerial"] = {"rich_text": [{"text": {"content": str(next_serial)}}]}
            else:
                # default/title
                properties["sysSerial"] = {"title": [{"text": {"content": str(next_serial)}}]}
            properties["date"] = {"date": {"start": date_iso}}

            create_resp = requests.post(
                "https://api.notion.com/v1/pages",
                headers=NOTION_HEADERS,
                json={
                    "parent": {
                        "type": "data_source_id",
                        "data_source_id": NOTION_DATA_SOURCE_ID
                    },
                    "properties": properties
                },
            )

            if create_resp.status_code not in (200, 201):
                # 嘗試重試，並回傳 log
                try:
                    push_log_to_source(source_id, f"Notion create failed (status {create_resp.status_code}). Attempt {attempt}.")
                except Exception:
                    pass
                continue

            created = create_resp.json()
            created_page_id = created.get("id")
            try:
                push_log_to_source(source_id, f"Created Notion page {created_page_id} with sysSerial {next_serial} (attempt {attempt}).")
            except Exception:
                pass

            # 檢查是否有重複使用相同 sysSerial 的頁面
            try:
                # 先嘗試以 filter 查詢
                chk_payload = {"filter": {"property": "sysSerial", "rich_text": {"equals": str(next_serial)}} , "page_size": 100}
                chk_r = requests.post(url, headers=NOTION_HEADERS, json=chk_payload)
                chk_results = chk_r.json().get("results", [])
            except Exception:
                chk_results = []

            # 若 filter 查不到，再以最近的 100 筆檢查（fallback）
            if not chk_results:
                try:
                    fallback_payload = {"sorts": [{"property": "sysSerial", "direction": "descending"}], "page_size": 100}
                    fb_r = requests.post(url, headers=NOTION_HEADERS, json=fallback_payload)
                    chk_results = fb_r.json().get("results", [])
                except Exception:
                    chk_results = []

            # 計算相同序號數量
            same_count = 0
            for res in chk_results:
                props_r = res.get("properties", {})
                s_prop_r = props_r.get("sysSerial", {})
                val = None
                if "title" in s_prop_r and s_prop_r.get("title"):
                    val = s_prop_r["title"][0].get("plain_text") or s_prop_r["title"][0].get("text", {}).get("content")
                elif "rich_text" in s_prop_r and s_prop_r.get("rich_text"):
                    val = s_prop_r["rich_text"][0].get("plain_text")
                elif "number" in s_prop_r and s_prop_r.get("number") is not None:
                    val = str(s_prop_r.get("number"))
                if val is not None and str(val) == str(next_serial):
                    same_count += 1

            if same_count <= 1:
                # 成功（沒有或只有自己）
                try:
                    push_log_to_source(source_id, f"sysSerial {next_serial} assigned OK (found {same_count} entries).")
                except Exception:
                    pass
                break

            # 發生重複：將剛建立的頁面序號遞增，繼續下一輪檢查
            try:
                # 發生重複：將剛建立的頁面序號遞增，繼續下一輪檢查
                push_log_to_source(source_id, f"Conflict detected for sysSerial {next_serial} ({same_count} pages). Incrementing to try to resolve.")
            except Exception:
                pass
            try:
                next_serial += 1
                # 依照目前 serial_type 更新格式
                if serial_type == "number":
                    patch_props = {"sysSerial": {"number": next_serial}}
                elif serial_type == "rich_text":
                    patch_props = {"sysSerial": {"rich_text": [{"text": {"content": str(next_serial)}}]}}
                else:
                    patch_props = {"sysSerial": {"title": [{"text": {"content": str(next_serial)}}]}}
                requests.patch(f"https://api.notion.com/v1/pages/{created_page_id}", headers=NOTION_HEADERS, json={"properties": patch_props})
            except Exception:
                # 若無法 patch，繼續下一次嘗試（下次會重新計算）
                pass

        # 迴圈結束：若仍未建立 page id，視為建立失敗
        if not created_page_id:
            # 無法建立頁面，回報給來源
            try:
                push_log_to_source(source_id, f"Failed to create Notion page after {max_attempts} attempts.")
            except Exception:
                pass
            # 直接回傳（上層會處理錯誤）
            return


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


def get_push_target(event):
    source = event.source
    if source.type == "group":
        return source.group_id
    elif source.type == "room":
        return source.room_id
    return source.user_id


def push_log_to_source(source_id, text):
    """Send a log message to the source (user/group/room) via LINE push_message.
    Silently ignore failures to avoid breaking main flow."""
    try:
        target = source_id
        if source_id.startswith("user_") or source_id.startswith("group_") or source_id.startswith("room_"):
            target = source_id.replace("user_", "").replace("group_", "").replace("room_", "")
        line_bot_api.push_message(target, TextSendMessage(text=text))
    except Exception:
        pass


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
        f"Translate the following text between {resolved_lang1} and {resolved_lang2}.\n"
        f"Rules:\n"
        f"1. If the text is in {resolved_lang1}, output only its {resolved_lang2} translation.\n"
        f"2. If the text is in {resolved_lang2}, output only its {resolved_lang1} translation.\n"
        f"3. If the text is in neither language, output exactly: ⚠️ 無法識別語言，請確認設定的語言是否正確。\n"
        f"4. Output ONLY the translated text. No language identification, no explanations, no notes, no extra context whatsoever.\n\n"
        f"Text to translate:\n{text}"
    )
    message = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
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
    push_target = get_push_target(event)

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
            line_bot_api.push_message(push_target, TextSendMessage(text=result))
        except LineBotApiError as e:
            if e.status_code == 429:
                msg = "⚠️ LINE訊息額度已用完，本月無法繼續傳送翻譯結果。"
            else:
                msg = f"❌ LINE API 錯誤：{e.error.message}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
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
