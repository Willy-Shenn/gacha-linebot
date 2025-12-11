from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
import os

# 讀取 .env
load_dotenv()

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    # 取得 X-Line-Signature header
    signature = request.headers.get('X-Line-Signature', '')

    # 取得 request body 文字
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"

# 收到文字訊息就原封不動回覆
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    reply_text = f"你說：{user_text}"
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    # 開發階段先用這個
    app.run(host="0.0.0.0", port=8000)
