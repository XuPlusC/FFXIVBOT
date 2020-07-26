import random
import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OTTERBOT_ROOT = os.environ.get("OTTERBOT_ROOT", BASE_DIR)
sys.path.append(OTTERBOT_ROOT)
os.environ["DJANGO_SETTINGS_MODULE"] = "Otter.settings"
from Otter import settings
import django
from django.db import transaction

django.setup()
from channels.layers import get_channel_layer
from channels.exceptions import StopConsumer

channel_layer = get_channel_layer()
import traceback
import pika
import urllib
from bs4 import BeautifulSoup
import logging
import hmac
import html
import codecs
import base64
import requests
import math
from hashlib import md5
import time
import re
import pytz
import datetime
from collections import OrderedDict
import json
from asgiref.sync import async_to_sync
import ffxivbot.handlers as handlers
from otterbot.models import *


USE_GRAFANA = getattr(settings, "USE_GRAFANA", False)
CONFIG_PATH = os.environ.get(
    "OTTERBOT_CONFIG", os.path.join(OTTERBOT_ROOT, "ffxivbot/config.json")
)


def handle_message(bot, message):
    new_message = message
    if isinstance(message, list):
        new_message = []
        for idx, msg in enumerate(message):
            if msg["type"] == "share" and bot.share_banned:
                share_data = msg["data"]
                new_message.append(
                    {
                        "type": "text",
                        "data": {
                            "text": "{}\n{}\n{}".format(
                                share_data["title"],
                                share_data["content"],
                                share_data["url"],
                            )
                        },
                    }
                )
            else:
                new_message.append(msg)
    return new_message


def call_api(bot, action, params, echo=None, **kwargs):
    if "async" not in action and not echo:
        action = action + "_async"
    if "send_" in action and "_msg" in action:
        params["message"] = handle_message(bot, params["message"])
    jdata = {"action": action, "params": params}
    if echo:
        jdata["echo"] = echo
    post_type = kwargs.get("post_type", "websocket")
    if post_type == "websocket":
        async_to_sync(channel_layer.send)(
            bot.api_channel_name, {"type": "send.event", "text": json.dumps(jdata)}
        )
    elif post_type == "http":
        url = os.path.join(
            bot.api_post_url, "{}?access_token={}".format(action, bot.access_token)
        )
        headers = {"Content-Type": "application/json"}
        r = requests.post(url=url, headers=headers, data=json.dumps(params), timeout=5)
        if r.status_code != 200:
            print("HTTP Callback failed:")
            print(r.text)
    elif post_type == "wechat":
        print("calling api:{}".format(action))

        def req_url(params):
            url = "https://ex-api.botorange.com/message/send"
            headers = {"Content-Type": "application/json"}
            print("params:{}".format(json.dumps(params)))
            r = requests.post(
                url=url, headers=headers, data=json.dumps(params), timeout=5
            )
            if r.status_code != 200:
                print("Wechat HTTP Callback failed:")
                print(r.text)

        config = json.load(open(CONFIG_PATH, encoding="utf-8"))
        params["chatId"] = kwargs.get("chatId", "")
        params["token"] = config.get("WECHAT_TOKEN", "")
        if "send_" in action and "_msg" in action:
            if isinstance(params["message"], str):
                text = params["message"]
                at = re.finditer(r"\[CQ:at,qq=(.*)\]", text)
                if at:
                    params["mention"] = [at_m.group(1) for at_m in at]
                text = re.sub(r"\[CQ:at,qq=(.*)\]", "", text)
                img_r = r"\[CQ:image,file=(.*?)(?:\]|,.*?\])"
                img_m = re.search(img_r, text)
                if img_m:  # FIXME: handle text & img message
                    params["messageType"] = 1
                    params["payload"] = {"url": img_m.group(1)}
                else:
                    params["messageType"] = 0
                    params["payload"] = {"text": text.strip()}
                req_url(params)
            else:
                for msg_seg in params["message"]:
                    if msg_seg["type"] == "image":
                        params["messageType"] = 1
                        params["payload"] = {"url": msg_seg["data"]["file"]}
                        req_url(params)
                    elif msg_seg["type"] == "text":
                        params["messageType"] = 0
                        params["payload"] = {"text": msg_seg["data"]["text"].strip()}
                        req_url(params)
                    time.sleep(1)


def send_message(bot, private_group, uid, message, **kwargs):
    if private_group == "group":
        call_api(bot, "send_group_msg", {"group_id": uid, "message": message}, **kwargs)
    elif private_group == "discuss":
        call_api(
            bot, "send_discuss_msg", {"discuss_id": uid, "message": message}, **kwargs
        )
    elif private_group == "private":
        call_api(
            bot, "send_private_msg", {"user_id": uid, "message": message}, **kwargs
        )


def update_group_member_list(bot, group_id, **kwargs):
    call_api(
        bot,
        "get_group_member_list",
        {"group_id": group_id},
        "get_group_member_list:%s" % (group_id),
        **kwargs,
    )


class PikaException(Exception):
    def __init__(self, message="Default PikaException"):
        Exception.__init__(self, message)


# LOG_FORMAT = ('%(levelname) -10s %(asctime)s %(name) -30s %(funcName) '
#               '-35s %(lineno) -5d: %(message)s')
LOGGER = logging.getLogger(__name__)


class PikaConsumer(object):

    EXCHANGE = "message"
    EXCHANGE_TYPE = "topic"
    QUEUE = "otterbot"
    ROUTING_KEY = ""

    def __init__(self, amqp_url):
        self._connection = None
        self._channel = None
        self._closing = False
        self._consumer_tag = None
        self._url = amqp_url

    def connect(self):
        LOGGER.info("Connecting to %s", self._url)
        parameters = pika.URLParameters(self._url)

        return pika.SelectConnection(
            parameters, self.on_connection_open, stop_ioloop_on_close=False
        )

    def on_connection_open(self, unused_connection):
        LOGGER.info("Connection opened")
        self.add_on_connection_close_callback()
        self.open_channel()

    def add_on_connection_close_callback(self):
        LOGGER.info("Adding connection close callback")
        self._connection.add_on_close_callback(self.on_connection_closed)

    def on_connection_closed(self, connection, reply_code, reply_text):
        self._channel = None
        if self._closing:
            self._connection.ioloop.stop()
        else:
            LOGGER.warning(
                "Connection closed, reopening in 5 seconds: (%s) %s",
                reply_code,
                reply_text,
            )
            self._connection.add_timeout(5, self.reconnect)

    def reconnect(self):
        self._connection.ioloop.stop()
        if not self._closing:
            self._connection = self.connect()
            self._connection.ioloop.start()

    def open_channel(self):
        LOGGER.info("Creating a new channel")
        self._connection.channel(on_open_callback=self.on_channel_open)

    def on_channel_open(self, channel):
        LOGGER.info("Channel opened")
        self._channel = channel
        self._channel.basic_qos(prefetch_count=1)
        self.add_on_channel_close_callback()
        self.setup_exchange(self.EXCHANGE)

    def add_on_channel_close_callback(self):
        LOGGER.info("Adding channel close callback")
        self._channel.add_on_close_callback(self.on_channel_closed)

    def on_channel_closed(self, channel, reply_code, reply_text):
        LOGGER.warning(
            "Channel %i was closed: (%s) %s", channel, reply_code, reply_text
        )
        self._connection.close()

    def setup_exchange(self, exchange_name):
        LOGGER.info("Declaring exchange %s", exchange_name)
        self._channel.exchange_declare(
            self.on_exchange_declareok, exchange_name, self.EXCHANGE_TYPE
        )

    def on_exchange_declareok(self, unused_frame):
        LOGGER.info("Exchange declared")
        self.setup_queue(self.QUEUE)

    def setup_queue(self, queue_name):
        LOGGER.info("Declaring queue %s", queue_name)
        self._channel.queue_declare(
            self.on_queue_declareok,
            queue_name,
            arguments={"x-max-priority": 20, "x-message-ttl": 60000},
        )

    def on_queue_declareok(self, method_frame):
        LOGGER.info(
            "Binding %s to %s with %s", self.EXCHANGE, self.QUEUE, self.ROUTING_KEY
        )
        self._channel.queue_bind(
            self.on_bindok, self.QUEUE, self.EXCHANGE, self.ROUTING_KEY
        )

    def on_bindok(self, unused_frame):
        LOGGER.info("Queue bound")
        self.start_consuming()

    def start_consuming(self):
        LOGGER.info("Issuing consumer related RPC commands")
        self.add_on_cancel_callback()
        self._consumer_tag = self._channel.basic_consume(self.on_message, self.QUEUE)

    def add_on_cancel_callback(self):
        LOGGER.info("Adding consumer cancellation callback")
        self._channel.add_on_cancel_callback(self.on_consumer_cancelled)

    def on_consumer_cancelled(self, method_frame):
        LOGGER.info("Consumer was cancelled remotely, shutting down: %r", method_frame)
        if self._channel:
            self._channel.close()

    def on_message(self, unused_channel, basic_deliver, properties, body):
        try:
            receive = json.loads(body)
            receive["pika_time"] = time.time()
            self_id = receive["self_id"]
            try:
                bot = QQBot.objects.get(user_id=self_id)
            except QQBot.DoesNotExist as e:
                LOGGER.error("QQBot {} does not exsit.".format(self_id))
                raise e
            config = json.load(open(CONFIG_PATH, encoding="utf-8"))
            already_reply = False
            # heart beat
            if (
                receive["post_type"] == "meta_event"
                and receive["meta_event_type"] == "heartbeat"
            ):
                LOGGER.debug(
                    "bot:{} Event heartbeat at time:{}".format(
                        self_id, int(time.time())
                    )
                )
                call_api(
                    bot,
                    "get_status",
                    {},
                    "get_status:{}".format(self_id),
                    post_type=receive.get("reply_api_type", "websocket"),
                )

            if receive["post_type"] == "message":
                user_id = receive["user_id"]
                # don't reply to another bot
                if QQBot.objects.filter(user_id=user_id).exists():
                    raise PikaException(
                        "{} reply from another bot:{}".format(
                            receive["self_id"], user_id
                        )
                    )
                (user, created) = QQUser.objects.get_or_create(user_id=user_id)
                if 0 < time.time() < user.ban_till:
                    raise PikaException(
                        "User {} get banned till {}".format(user_id, user.ban_till)
                    )

                # replace alter commands
                for (alter_command, command) in handlers.alter_commands.items():
                    if receive["message"].find(alter_command) == 0:
                        receive["message"] = receive["message"].replace(
                            alter_command, command, 1
                        )
                        break

                group_id = None
                group = None
                group_created = False
                discuss_id = None
                # Group Control Func
                if receive["message"].find("\\") == 0:
                    receive["message"] = receive["message"].replace("\\", "/", 1)
                if receive["message_type"] == "discuss":
                    discuss_id = receive["discuss_id"]
                if receive["message_type"] == "group":
                    group_id = receive["group_id"]
                    (group, group_created) = QQGroup.objects.get_or_create(
                        group_id=group_id
                    )
                    # self-ban in group
                    if int(time.time()) < group.ban_till:
                        raise PikaException(
                            "{} banned by group:{}".format(self_id, group_id)
                        )
                        # LOGGER.info("{} banned by group:{}".format(self_id, group_id))
                        # self.acknowledge_message(basic_deliver.delivery_tag)
                        # return
                    group_commands = json.loads(group.commands)

                    try:
                        member_list = json.loads(group.member_list)
                        if group_created or not member_list:
                            update_group_member_list(
                                bot,
                                group_id,
                                post_type=receive.get("reply_api_type", "websocket"),
                            )
                    except json.decoder.JSONDecodeError:
                        member_list = []

                    if receive["message"].find("/group_help") == 0:
                        msg = (
                            ""
                            if member_list
                            else "本群成员信息获取失败，请尝试重启酷Q并使用/update_group刷新群成员信息\n"
                        )
                        for (k, v) in handlers.group_commands.items():
                            command_enable = True
                            if group and group_commands:
                                command_enable = (
                                    group_commands.get(k, "enable") == "enable"
                                )
                            if command_enable:
                                msg += "{}: {}\n".format(k, v)
                        msg = msg.strip()
                        send_message(
                            bot,
                            receive["message_type"],
                            discuss_id or group_id or user_id,
                            msg,
                            post_type=receive.get("reply_api_type", "websocket"),
                            chatId=receive.get("chatId", ""),
                        )
                    else:
                        if receive["message"].find("/update_group") == 0:
                            update_group_member_list(
                                bot,
                                group_id,
                                post_type=receive.get("reply_api_type", "websocket"),
                            )
                        # get sender's user_info
                        user_info = receive.get("sender")
                        user_info = (
                            user_info
                            if (user_info and ("role" in user_info.keys()))
                            else None
                        )
                        if member_list and not user_info:
                            for item in member_list:
                                if str(item["user_id"]) == str(user_id):
                                    user_info = item
                                    break
                        if not user_info:
                            if receive.get("reply_api_type", "websocket") == "wechat":
                                user_info = {
                                    "user_id": receive["user_id"],
                                    "nickname": receive["data"]["contactName"],
                                    "role": "member",
                                }
                                if receive["user_id"] not in list(
                                    map(lambda x: x["user_id"], member_list)
                                ):
                                    member_list.append(user_info)
                                    group.member_list = json.dumps(member_list)
                                    group.save(update_fields=["member_list"])
                            else:
                                raise PikaException(
                                    "No user info for user_id:{} in group:{}".format(
                                        user_id, group_id
                                    )
                                )
                            # LOGGER.error("No user info for user_id:{} in group:{}".format(user_id, group_id))
                            # self.acknowledge_message(basic_deliver.delivery_tag)
                            # return

                        group_command_keys = sorted(
                            handlers.group_commands.keys(), key=lambda x: -len(x)
                        )
                        for command_key in group_command_keys:
                            if receive["message"].find(command_key) == 0:
                                if (
                                    receive["message_type"] == "group"
                                    and group_commands
                                ):
                                    if (
                                        command_key in group_commands.keys()
                                        and group_commands[command_key] == "disable"
                                    ):
                                        continue
                                if not group.registered and command_key != "/group":
                                    msg = "本群%s未在数据库注册，请群主使用/register_group命令注册" % (
                                        group_id
                                    )
                                    send_message(
                                        bot,
                                        "group",
                                        group_id,
                                        msg,
                                        post_type=receive.get(
                                            "reply_api_type", "websocket"
                                        ),
                                        chatId=receive.get("chatId", ""),
                                    )
                                    break
                                else:
                                    handle_method = getattr(
                                        handlers,
                                        "QQGroupCommand_{}".format(
                                            command_key.replace("/", "", 1)
                                        ),
                                    )
                                    action_list = handle_method(
                                        receive=receive,
                                        global_config=config,
                                        bot=bot,
                                        user_info=user_info,
                                        member_list=member_list,
                                        group=group,
                                        commands=handlers.commands,
                                        group_commands=handlers.group_commands,
                                        alter_commands=handlers.alter_commands,
                                    )
                                    if USE_GRAFANA:
                                        command_log = CommandLog(
                                            time=int(time.time()),
                                            bot_id=str(self_id),
                                            user_id=str(user_id),
                                            group_id=str(group_id),
                                            command=str(command_key),
                                            message=receive["message"],
                                        )
                                        command_log.save()
                                    for action in action_list:
                                        call_api(
                                            bot,
                                            action["action"],
                                            action["params"],
                                            echo=action["echo"],
                                            post_type=receive.get(
                                                "reply_api_type", "websocket"
                                            ),
                                            chatId=receive.get("chatId", ""),
                                        )
                                        already_reply = True
                                    if already_reply:
                                        break

                if receive["message"].find("/help") == 0:
                    msg = ""
                    for (k, v) in handlers.commands.items():
                        command_enable = True
                        if group and group_commands:
                            command_enable = group_commands.get(k, "enable") == "enable"
                        if command_enable:
                            msg += "{}: {}\n".format(k, v)
                    msg += "具体介绍详见Wiki使用手册: {}\n".format(
                        "https://github.com/Bluefissure/OTTERBOT/wiki/"
                    )
                    msg = msg.strip()
                    send_message(
                        bot,
                        receive["message_type"],
                        group_id or user_id,
                        msg,
                        post_type=receive.get("reply_api_type", "websocket"),
                        chatId=receive.get("chatId", ""),
                    )

                if receive["message"].find("/ping") == 0:
                    msg = ""
                    if "detail" in receive["message"]:
                        msg += "[CQ:at,qq={}]\ncoolq->server: {:.2f}s\nserver->rabbitmq: {:.2f}s\nhandle init: {:.2f}s".format(
                            receive["user_id"],
                            receive["consumer_time"] - receive["time"],
                            receive["pika_time"] - receive["consumer_time"],
                            time.time() - receive["pika_time"],
                        )
                    else:
                        msg += "[CQ:at,qq={}] {:.2f}s".format(
                            receive["user_id"], time.time() - receive["time"]
                        )
                    msg = msg.strip()
                    LOGGER.info("{} calling command: {}".format(user_id, "/ping"))
                    print(("{} calling command: {}".format(user_id, "/ping")))
                    send_message(
                        bot,
                        receive["message_type"],
                        discuss_id or group_id or user_id,
                        msg,
                        post_type=receive.get("reply_api_type", "websocket"),
                        chatId=receive.get("chatId", ""),
                    )

                command_keys = sorted(handlers.commands.keys(), key=lambda x: -len(x))
                for command_key in command_keys:
                    if receive["message"].find(command_key) == 0:
                        if receive["message_type"] == "group" and group_commands:
                            if (
                                command_key in group_commands.keys()
                                and group_commands[command_key] == "disable"
                            ):
                                continue
                        handle_method = getattr(
                            handlers,
                            "QQCommand_{}".format(command_key.replace("/", "", 1)),
                        )
                        action_list = handle_method(
                            receive=receive, global_config=config, bot=bot
                        )
                        if USE_GRAFANA:
                            command_log = CommandLog(
                                time=int(time.time()),
                                bot_id=str(self_id),
                                user_id=str(user_id),
                                group_id="private"
                                if receive["message_type"] != "group"
                                else str(group_id),
                                command=str(command_key),
                                message=receive["message"],
                            )
                            command_log.save()
                        for action in action_list:
                            call_api(
                                bot,
                                action["action"],
                                action["params"],
                                echo=action["echo"],
                                post_type=receive.get("reply_api_type", "websocket"),
                                chatId=receive.get("chatId", ""),
                            )
                            already_reply = True
                        break

                # handling chat
                if receive["message_type"] == "group":
                    if not already_reply:
                        action_list = handlers.QQGroupChat(
                            receive=receive,
                            global_config=config,
                            bot=bot,
                            user_info=user_info,
                            member_list=member_list,
                            group=group,
                            commands=handlers.commands,
                            alter_commands=handlers.alter_commands,
                        )
                        # need fix: disable chat logging for a while
                        # if USE_GRAFANA:
                        #     command_log = CommandLog(
                        #         time = int(time.time()),
                        #         bot_id = str(self_id),
                        #         user_id = str(user_id),
                        #         group_id = "private" if receive["message_type"] != "group" else str(group_id),
                        #         command = "/chat",
                        #         message = receive["message"]
                        #     )
                        #     command_log.save()
                        for action in action_list:
                            call_api(
                                bot,
                                action["action"],
                                action["params"],
                                echo=action["echo"],
                                post_type=receive.get("reply_api_type", "websocket"),
                                chatId=receive.get("chatId", ""),
                            )

            CONFIG_GROUP_ID = config["CONFIG_GROUP_ID"]
            if receive["post_type"] == "request":
                if receive["request_type"] == "friend":  # Add Friend
                    qq = receive["user_id"]
                    flag = receive["flag"]
                    if bot.auto_accept_friend:
                        reply_data = {"flag": flag, "approve": True}
                        call_api(
                            bot,
                            "set_friend_add_request",
                            reply_data,
                            post_type=receive.get("reply_api_type", "websocket"),
                        )
                if (
                    receive["request_type"] == "group"
                    and receive["sub_type"] == "invite"
                ):  # Invite Group
                    flag = receive["flag"]
                    if bot.auto_accept_invite:
                        reply_data = {
                            "flag": flag,
                            "sub_type": "invite",
                            "approve": True,
                        }
                        call_api(
                            bot,
                            "set_group_add_request",
                            reply_data,
                            post_type=receive.get("reply_api_type", "websocket"),
                        )
                if (
                    receive["request_type"] == "group"
                    and receive["sub_type"] == "add"
                    and str(receive["group_id"]) == CONFIG_GROUP_ID
                ):  # Add Group
                    flag = receive["flag"]
                    user_id = receive["user_id"]
                    qs = QQBot.objects.filter(owner_id=user_id)
                    if qs.count() > 0:
                        reply_data = {"flag": flag, "sub_type": "add", "approve": True}
                        call_api(
                            bot,
                            "set_group_add_request",
                            reply_data,
                            post_type=receive.get("reply_api_type", "websocket"),
                        )
                        reply_data = {
                            "group_id": CONFIG_GROUP_ID,
                            "user_id": user_id,
                            "special_title": "饲养员",
                        }
                        call_api(
                            bot,
                            "set_group_special_title",
                            reply_data,
                            post_type=receive.get("reply_api_type", "websocket"),
                        )
            if receive["post_type"] == "event":
                if receive["event"] == "group_increase":
                    group_id = receive["group_id"]
                    user_id = receive["user_id"]
                    try:
                        group = QQGroup.objects.get(group_id=group_id)
                        msg = group.welcome_msg.strip()
                        if msg != "":
                            msg = "[CQ:at,qq=%s]" % (user_id) + msg
                            send_message(
                                bot,
                                "group",
                                group_id,
                                msg,
                                post_type=receive.get("reply_api_type", "websocket"),
                                chatId=receive.get("chatId", ""),
                            )
                    except Exception as e:
                        traceback.print_exc()
            # print(" [x] Received %r" % body)
        except PikaException as pe:
            traceback.print_exc()
            LOGGER.error(pe)
        except Exception as e:
            traceback.print_exc()
            LOGGER.error(e)

        self.acknowledge_message(basic_deliver.delivery_tag)

    def acknowledge_message(self, delivery_tag):
        LOGGER.info("pid:%s Acknowledging message %s", os.getpid(), delivery_tag)
        self._channel.basic_ack(delivery_tag)

    def stop_consuming(self):
        if self._channel:
            LOGGER.info("Sending a Basic.Cancel RPC command to RabbitMQ")
            self._channel.basic_cancel(self.on_cancelok, self._consumer_tag)

    def on_cancelok(self, unused_frame):
        LOGGER.info("RabbitMQ acknowledged the cancellation of the consumer")
        self.close_channel()

    def close_channel(self):
        LOGGER.info("Closing the channel")
        self._channel.close()

    def run(self):
        self._connection = self.connect()
        self._connection.ioloop.start()

    def stop(self):
        LOGGER.info("Stopping")
        self._closing = True
        self.stop_consuming()
        self._connection.ioloop.start()
        LOGGER.info("Stopped")

    def close_connection(self):
        LOGGER.info("Closing connection")
        self._connection.close()


def main():
    logging.basicConfig(level=logging.INFO)
    pikapika = PikaConsumer("amqp://guest:guest@localhost:5672/?heartbeat=600")
    try:
        pikapika.run()
    except KeyboardInterrupt:
        pikapika.stop()


if __name__ == "__main__":
    main()