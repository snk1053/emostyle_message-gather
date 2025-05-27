import os
import re
import json
import requests
from slack_bolt import App
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
import logging
import tempfile

#-------------------------------------------------
# 必須環境変数
#   SLACK_BOT_TOKEN      : xoxb-…
#   SLACK_SIGNING_SECRET : Signing Secret
#   ALL_TIMELINE_ID      : #auto_timeline の Channel ID
#-------------------------------------------------

ALL_TIMELINE = os.environ["ALL_TIMELINE_ID"]

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)
client: WebClient = app.client

# ルートメッセージ ts → タイムライン側 ts
relay_map: dict[str, str] = {}
# channel_id → channel_name キャッシュ
channel_name_cache: dict[str, str] = {}
# user_id → user_info キャッシュ
user_info_cache: dict[str, dict] = {}

#-------------------------------------------------
# 公開チャンネルへ JOIN（起動時）
#-------------------------------------------------
def invite_all_public_channels():
    cursor = None
    while True:
        res = client.conversations_list(types="public_channel",
                                        limit=1000,
                                        cursor=cursor,
                                        exclude_archived=True)
        for ch in res["channels"]:
            if not ch["is_member"]:
                try:
                    client.conversations_join(channel=ch["id"])
                except SlackApiError as e:
                    if e.response["error"] in ("already_in_channel", "is_archived"):
                        continue
                    raise
        cursor = res.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

#-------------------------------------------------
# チャンネル名を取得（キャッシュ付き）
#-------------------------------------------------
def get_channel_name(ch_id: str) -> str:
    if ch_id in channel_name_cache:
        return channel_name_cache[ch_id]
    try:
        info = client.conversations_info(channel=ch_id)["channel"]
        name = info["name"]
        channel_name_cache[ch_id] = name
        return name
    except SlackApiError as e:
        if hasattr(e, 'response') and e.response.get("error") == "channel_not_found":
            channel_name_cache[ch_id] = f"external-{ch_id[-6:]}"
            return channel_name_cache[ch_id]
        return "unknown"

#-------------------------------------------------
# ユーザー情報を取得（キャッシュ付き）
#-------------------------------------------------
def get_user_info(user_id: str) -> dict:
    if user_id in user_info_cache:
        return user_info_cache[user_id]
    try:
        info = client.users_info(user=user_id)["user"]
        user_info_cache[user_id] = info
        return info
    except SlackApiError:
        return {"real_name": "Unknown User", "profile": {"image_48": ""}}

#-------------------------------------------------
# 転記リンク用ペイロード
#-------------------------------------------------
def make_payload(ch_id: str, ch_type: str, permalink: str) -> str:
    ch_name = get_channel_name(ch_id)
    ch_display = f"#{ch_name}"
    return f"{ch_display} を <{permalink}|元投稿リンクはこちら>"

#-------------------------------------------------
# Unfurl 用ブロック
#-------------------------------------------------
def build_unfurl_block(event: dict, include_images: bool = True, logger = None) -> list:
    blocks = []

    # ユーザー情報を取得
    user_id = event.get("user")
    if user_id:
        user_info = get_user_info(user_id)
        display_name = user_info.get("real_name", "Unknown User")
        image_url = user_info.get("profile", {}).get("image_48", "")

        if image_url:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "image",
                        "image_url": image_url,
                        "alt_text": display_name
                    },
                    {
                        "type": "plain_text",
                        "text": f"{display_name} の投稿"
                    }
                ]
            })
        else:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "plain_text",
                        "text": f"{display_name} の投稿"
                    }
                ]
            })
    else:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": "誰かの投稿"
                }
            ]
        })

    # 投稿内容のブロック
    text = event.get("text") or "(本文なし)"

    # メンションを置き換え
    def replace_mention(match):
        mention_user_id = match.group(1)
        user_info = get_user_info(mention_user_id)
        return f"@{user_info.get('real_name', 'Unknown')}"

    text = re.sub(r'<@([A-Z0-9]+)>', replace_mention, text)

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": text}
    })

    # チャンネル情報を別のコンテキストブロックとして追加
    ch_name = get_channel_name(event["channel"])
    is_private = event.get("channel_type") == "group"
    if is_private:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": f"プライベートチャンネル: {ch_name}",
                    "emoji": True
                }
            ]
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": f"チャンネル: #{ch_name}",
                    "emoji": True
                }
            ]
        })

    # ファイル添付の処理
    if "files" in event and include_images:
        for f in event.get("files", []):
            file_id = f.get("id")
            file_name = f.get("name", "ファイル")
            file_type = f.get("mimetype", "")

            if logger:
                logger.info(f"Processing file: {file_id}, name={file_name}, type={file_type}")

            if file_type.startswith("image/"):
                if is_private and logger:
                    logger.info(f"Processing private channel image: {file_id}")

                    reuploaded_file = download_and_reupload_file(f, logger)

                    if reuploaded_file:
                        logger.info(f"Successfully reuploaded file: {reuploaded_file.get('id')}")

                        url_private = reuploaded_file.get("url_private")
                        permalink = reuploaded_file.get("permalink")

                        blocks.append({
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"📷 *画像が添付されています*: <{permalink}|{file_name}>"
                            }
                        })
                        logger.info("Image link block added successfully")
                    else:
                        logger.error(f"Failed to reupload file: {file_id}")

                        url_private = f.get("url_private")
                        if url_private:
                            blocks.append({
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"📷 *画像が添付されています* (プライベートチャンネル): <{url_private}|{file_name}>"
                                }
                            })
                        else:
                            blocks.append({
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"📷 *画像が添付されています* (表示できません)"
                                }
                            })
                else:
                    file_url = f.get("url_private")
                    permalink = f.get("permalink")

                    if logger:
                        logger.info(f"Processing public channel image: {file_url}")

                    blocks.append({
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"📷 *画像が添付されています*: <{permalink or file_url}|{file_name}>"
                        }
                    })
            else:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📎 *添付ファイル:* <{f.get('url_private')}|{file_name}>"
                    }
                })

    return blocks

#-------------------------------------------------
# message.channels と message.groups を処理
#-------------------------------------------------
@app.event("message")
def relay(event, logger):
    if (event.get("subtype") == "bot_message" and event.get("username") != "Slackbot") or event["channel"] == ALL_TIMELINE:
        return

    is_slackbot_file_share = (event.get("subtype") == "bot_message" and
                             event.get("username") == "Slackbot" and
                             "さんがあなたのプライベートファイル" in event.get("text", ""))

    try:
        event_debug = {k: v for k, v in event.items() if k != "files"}
        if "files" in event:
            event_debug["files_count"] = len(event["files"])
        logger.info(f"Event details: {json.dumps(event_debug, default=str, ensure_ascii=False)}")
    except Exception as e:
        logger.error(f"Error logging event details: {e}")

    if "files" in event:
        logger.info(f"Files detected: {len(event['files'])} files")
        for i, f in enumerate(event["files"]):
            logger.info(f"File {i+1}: id={f.get('id')}, type={f.get('mimetype')}, name={f.get('name')}")

    logger.info(f"Processing message event: channel={event['channel']}, channel_type={event.get('channel_type')}, subtype={event.get('subtype')}")

    try:
        link = client.chat_getPermalink(channel=event["channel"],
                                        message_ts=event["ts"])["permalink"]
        logger.info(f"Got permalink: {link}")
    except Exception as e:
        logger.error(f"Error getting permalink: {e}")
        return

    if is_slackbot_file_share:
        logger.info("Skipping Slackbot file share message")
        return

    payload = make_payload(event["channel"],
                           event.get("channel_type", "channel"),
                           link)

    is_private = event.get("channel_type") == "group"

    try:
        if "thread_ts" not in event or event["thread_ts"] == event["ts"]:
            logger.info("Posting root message")

            if is_private:
                logger.info("Processing private channel message")
                unfurl_blocks = build_unfurl_block(event, include_images=True, logger=logger)

                logger.info(f"Generated {len(unfurl_blocks)} blocks for private channel")
                try:
                    blocks_json = json.dumps(unfurl_blocks, default=str, ensure_ascii=False)
                    logger.info(f"Blocks JSON: {blocks_json[:500]}..." if len(blocks_json) > 500 else blocks_json)
                except Exception as e:
                    logger.error(f"Error serializing blocks: {e}")

                try:
                    res = client.chat_postMessage(
                        channel=ALL_TIMELINE,
                        text=payload,
                        attachments=[{
                            "blocks": unfurl_blocks,
                            "color": "#f2c744"
                        }]
                    )
                    logger.info(f"Posted message to timeline: {res.get('ts')}")
                    relay_map[event["ts"]] = res["ts"]
                except Exception as e:
                    logger.error(f"Error posting private channel message: {e}")
                    import traceback
                    logger.error(f"Detailed error: {traceback.format_exc()}")
            else:
                logger.info("Processing public channel message")
                res = client.chat_postMessage(channel=ALL_TIMELINE, text=payload)
                relay_map[event["ts"]] = res["ts"]

                unfurl_blocks = build_unfurl_block(event, include_images=False)
                logger.info("Attempting to unfurl")
                try:
                    client.chat_unfurl(channel=ALL_TIMELINE,
                                      ts=res["ts"],
                                      unfurls={link: {"blocks": unfurl_blocks}})
                    logger.info("Unfurl successful")
                except Exception as e:
                    logger.error(f"Error in unfurl: {e}")

        else:
            root_ts = relay_map.get(event["thread_ts"])
            if root_ts:
                logger.info(f"Posting thread reply to {root_ts}")

                if is_private:
                    logger.info("Processing private channel thread reply")
                    unfurl_blocks = build_unfurl_block(event, include_images=True, logger=logger)
                    try:
                        res = client.chat_postMessage(
                            channel=ALL_TIMELINE,
                            thread_ts=root_ts,
                            text=payload,
                            attachments=[{
                                "blocks": unfurl_blocks,
                                "color": "#f2c744"
                            }]
                        )
                        logger.info(f"Posted thread reply: {res.get('ts')}")
                    except Exception as e:
                        logger.error(f"Error posting private channel thread reply: {e}")
                else:
                    logger.info("Processing public channel thread reply")
                    res = client.chat_postMessage(
                        channel=ALL_TIMELINE,
                        thread_ts=root_ts,
                        text=payload
                    )

                    unfurl_blocks = build_unfurl_block(event, include_images=False)
                    logger.info("Attempting to unfurl thread reply")
                    try:
                        client.chat_unfurl(channel=ALL_TIMELINE,
                                          ts=res["ts"],
                                          unfurls={link: {"blocks": unfurl_blocks}})
                        logger.info("Thread reply unfurl successful")
                    except Exception as e:
                        logger.error(f"Error in thread reply unfurl: {e}")
    except Exception as e:
        logger.error(f"Error in relay: {e}")
        import traceback
        logger.error(f"Detailed error: {traceback.format_exc()}")

#-------------------------------------------------
# ファイル共有イベントを処理
#-------------------------------------------------
@app.event("file_shared")
def handle_file_shared(event, logger):
    try:
        logger.info(f"File shared event received: {event}")
        file_id = event.get("file_id")
        if not file_id:
            logger.error("No file_id in the event")
            return

        try:
            file_info = client.files_info(file=file_id)
            logger.info(f"File info retrieved: {file_info.get('file', {}).get('name')} ({file_info.get('file', {}).get('mimetype')})")

        except SlackApiError as e:
            logger.error(f"Error getting file info: {e}")
    except Exception as e:
        logger.error(f"Error in file_shared event handler: {e}")
        import traceback
        logger.error(f"Detailed error: {traceback.format_exc()}")

#-------------------------------------------------
# Flask エンドポイント
#-------------------------------------------------
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

#-------------------------------------------------
# 起動
#-------------------------------------------------
if __name__ == "__main__":
    invite_all_public_channels()
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

#-------------------------------------------------
# ファイルをダウンロードして再アップロードする
#-------------------------------------------------
def download_and_reupload_file(file_info, logger):
    try:
        url = file_info.get("url_private")
        if not url:
            logger.error("No private URL found for file")
            return None

        file_name = file_info.get("name", "unknown_file")
        file_type = file_info.get("mimetype", "")

        logger.info(f"Downloading file: {file_name} ({file_type}) from {url}")

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file_name}") as temp_file:
                temp_path = temp_file.name

                headers = {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"}
                response = requests.get(url, headers=headers)

                if response.status_code != 200:
                    logger.error(f"Failed to download file: {response.status_code}")
                    os.unlink(temp_path)
                    return None

                temp_file.write(response.content)

            logger.info(f"File downloaded to: {temp_path}")

            try:
                with open(temp_path, "rb") as file_content:
                    upload_response = client.files_upload_v2(
                        channel_id=ALL_TIMELINE,
                        file=file_content,
                        filename=file_name
                    )

                logger.info(f"File reuploaded: {upload_response}")

                if upload_response and upload_response.get("file"):
                    return upload_response["file"]
            except Exception as e:
                logger.error(f"Error uploading file: {e}")
                import traceback
                logger.error(f"Upload error details: {traceback.format_exc()}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    logger.info(f"Temporary file deleted: {temp_path}")
                except Exception as e:
                    logger.error(f"Error deleting temporary file: {e}")
    except Exception as e:
        logger.error(f"Error in download_and_reupload: {e}")
        import traceback
        logger.error(f"Detailed error: {traceback.format_exc()}")

    return None
