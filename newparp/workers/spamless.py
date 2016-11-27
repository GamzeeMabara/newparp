#!/usr/bin/python

import multiprocessing
import hashlib
import json
import re
import signal
import time

from random import randint
from redis import StrictRedis
from sqlalchemy import and_, func
from sqlalchemy.orm.exc import NoResultFound

from newparp.helpers.chat import send_message
from newparp.model import sm, AnyChat, ChatUser, Message, User, SpamlessFilter
from newparp.model.connections import redis_pool


processes = []
queue = multiprocessing.Queue()

class Mark(Exception):
    pass


class Silence(Exception):
    pass


class Processor(multiprocessing.Process):
    def __init__(self, queue, shared):
        multiprocessing.Process.__init__(self)
        self.queue = queue
        self.shared = shared

        self.db = sm()
        self.redis = StrictRedis(connection_pool=redis_pool)
        self.lists = {}
        self.last_reload = shared["reload"]
        self.load_lists()

    def load_lists(self):
        print("reload", self.name)
        self.lists["banned_names"] = [
            re.compile(_.regex, re.IGNORECASE | re.MULTILINE)
            for _ in self.db.query(SpamlessFilter).filter(SpamlessFilter.type == "banned_names").all()
        ]
        self.lists["blacklist"] = [
            (re.compile(_.regex, re.IGNORECASE | re.MULTILINE), _.points)
            for _ in self.db.query(SpamlessFilter).filter(SpamlessFilter.type == "blacklist").all()
        ]
        self.lists["warnlist"] = [
            re.compile(_.regex, re.IGNORECASE | re.MULTILINE)
            for _ in self.db.query(SpamlessFilter).filter(SpamlessFilter.type == "warnlist").all()
        ]
        self.last_reload = shared["reload"]

    def run(self):
        while True:
            if self.last_reload != shared["reload"]:
                self.load_lists()

            ps_message = self.queue.get(block=True)
            self.process(ps_message)

    def process(self, ps_message):
        if ps_message["channel"] == "spamless:reload":
            shared["reload"] = int(time.time())
            self.load_lists()

        try:
            chat_id = int(ps_message["channel"].split(":")[1])
            data = json.loads(ps_message["data"])
        except (IndexError, KeyError, ValueError):
            return

        if "messages" not in data or len(data["messages"]) == 0:
            return

        for message in data["messages"]:

            if message["user_number"] is None:
                continue

            message["hash"] = hashlib.md5(
                ":".join([message["color"], message["acronym"], message["text"]])
                .encode("utf-8").lower()
            ).hexdigest()

            try:
                self.check_connection_spam(chat_id, message)
                self.check_banned_names(chat_id, message)
                self.check_message_filter(chat_id, message)
                self.check_warnlist(chat_id, message)

            except Mark as e:
                time.sleep(0.1)
                q = self.db.query(Message).filter(Message.id == message["id"]).update({"spam_flag": str(e)})
                message.update({"spam_flag": str(e)})
                self.redis.publish("spamless:live", json.dumps(message))
                self.db.commit()

            except Silence as e:
                time.sleep(0.1)

                # XXX maybe cache this?
                try:
                    chat_user, user, chat = self.db.query(
                        ChatUser, User, AnyChat,
                    ).join(
                        User, ChatUser.user_id == User.id,
                    ).join(
                        AnyChat, ChatUser.chat_id == AnyChat.id,
                    ).filter(and_(
                        ChatUser.chat_id == chat_id,
                        ChatUser.number == message["user_number"],
                    )).one()
                except NoResultFound:
                    continue

                if chat.type != "group":
                    flag_suffix = chat.type.upper()
                elif chat_user.computed_group in ("admin", "creator"):
                    flag_suffix = chat_user.computed_group.upper()
                else:
                    flag_suffix = "SILENCED"

                self.db.query(Message).filter(Message.id == message["id"]).update({
                    "spam_flag": str(e) + " " + flag_suffix,
                })

                message.update({"spam_flag": str(e) + " " + flag_suffix})
                self.redis.publish("spamless:live", json.dumps(message))

                if flag_suffix == "SILENCED":
                    self.db.query(ChatUser).filter(and_(
                        ChatUser.chat_id == chat_id,
                        ChatUser.number == message["user_number"]
                    )).update({"group": "silent"})
                    send_message(self.db, self.redis, Message(
                        chat_id=chat_id,
                        type="spamless",
                        name="The Spamless",
                        acronym="\u264b",
                        text="Spam has been detected and silenced. Please come [url=http://help.msparp.com/]here[/url] or ask a chat moderator to unsilence you if this was an accident.",
                        color="626262"
                    ), True)

                self.db.commit()

    def check_connection_spam(self, chat_id, message):
        if message["type"] not in ("join", "disconnect", "timeout", "user_info"):
            return
        attempts = increx(self.redis, "spamless:join:%s:%s" % (chat_id, message["user_number"]), expire=5)
        if attempts <= 10:
            return
        raise Silence("connection")

    def check_banned_names(self, chat_id, message):
        if not message["name"]:
            return
        lower_name = message["name"].lower()
        for name in self.lists["banned_names"]:
            if name.search(lower_name):
                raise Silence("name")

    def check_message_filter(self, chat_id, message):

        if message["type"] not in ("ic", "ooc", "me"):
            return

        message_key = "spamless:message:%s" % message["hash"]
        user_key = "spamless:blacklist:%s:%s" % (chat_id, message["user_number"])

        for phrase, points in self.lists["blacklist"]:
            total_points = len(phrase.findall(message["text"])) * int(points)
            increx(self.redis, message_key, expire=60, incr=total_points)
            increx(self.redis, user_key, expire=10, incr=total_points)

        message_attempts = increx(self.redis, message_key, expire=60)
        user_attempts = increx(self.redis, user_key, expire=10)

        if message_attempts >= randint(10, 35) or user_attempts >= 15:
            raise Silence("x%s" % max(message_attempts, user_attempts))
        elif message_attempts >= 10 or user_attempts >= 10:
            raise Mark("x%s" % max(message_attempts, user_attempts))

    def check_warnlist(self, chat_id, message):
        if message["type"] in ("join", "disconnect", "timeout"):
            return
        lower_text = message["text"].lower()
        for phrase in self.lists["warnlist"]:
            if phrase.search(lower_text):
                raise Mark("warnlist")


def increx(redis, key, expire=60, incr=1):
    result = redis.incr(key, incr)
    redis.expire(key, expire)
    return result


def halt(signal=None, frame=None):
    print("Caught signal %s" % signal)
    ps_thread.stop()
    for process in processes:
        process.terminate()


def on_ps(ps_message=None):
    queue.put(ps_message)

if __name__ == "__main__":

    redis = StrictRedis(connection_pool=redis_pool)
    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)

    ps = redis.pubsub(ignore_subscribe_messages=True)
    ps.psubscribe(**{"spamless:reload": on_ps, "channel:*": on_ps})
    ps_thread = ps.run_in_thread(sleep_time=0.01)

    with multiprocessing.Manager() as manager:
        shared = manager.dict()
        shared["reload"] = int(time.time())

        for x in range(0, 4):
            p = Processor(queue, shared)
            p.daemon = True
            processes.append(p)
            p.start()

    while ps_thread.is_alive():
        redis.setex("spamless:alive", 10, "alive")
        ps_thread.join(3)

