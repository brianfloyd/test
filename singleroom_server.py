#!/usr/bin/env python

import os
import uuid
import json
import re
import struct
import sqlite3

import tornado.ioloop
import tornado.web
from tornado import websocket
import logging
from tornado.log import enable_pretty_logging
from tornado.log import app_log
from tornado.util import bytes_type
from tornado.iostream import StreamClosedError
import tornado.options
from tornado import template 


def render_file(filename, args=None):
    loader = template.Loader("/")
    if args:
        data = loader.load(filename).generate(**args)
    else:
        data = loader.load(filename).generate()
    return data


class MainHandler(tornado.web.RequestHandler):
    def initialize(self, session_controller):
        """Store a reference to the "external" SessionController instance"""
        self._SC = session_controller

    def get(self):
        self.render("templates/main.html")


class SessionController(object):
    """Store data about connections, users, etc."""

    def __init__(self):
        self.clients = {}
        self.nicks = set([])
        self.instructor = None
        self._presentation_started = False
        self._follow_instructor = True
        self._lock_student_nav = True
        self._latest_instructor_state = ''

    def get_safe_nick(self, initial_nick):
        """Create a safe nick: one with no spaces, etc. and that doesn't exist already in the session."""
        safe_nick = re.sub(r"[^\w\s]", '', initial_nick)   
        safe_nick = re.sub(r"\s+", '', safe_nick)
        i = 1
        while safe_nick in self.nicks:
            safe_nick = safe_nick + str(i)
            i += 1
        return safe_nick

    def add_client(self, initial_nick, conn):
        """Store the websocket connection."""
        final_nick = self.get_safe_nick(initial_nick)
        client_obj = { "nick": final_nick, "connection": conn }
        # if it's the first participant, then make it the instructor
        if (len(self.clients)==0):
            client_obj['instructor'] = True
            self.instructor = client_obj
            conn.set_instructor(True)
        else:
            client_obj['instructor'] = False

        self.clients[final_nick] = client_obj   # we'll add state, and everything.
        self.nicks.add(final_nick)
        self.broadcast_nicklist()
        # Depending on the presentation status, send a 'wait' message to this conn, or tell it to load the pres. and go to a slide.
        return final_nick

    def remove_client(self, nick):
        """Remove all client information from the session handler."""
        self.clients[nick]['connection'].close()
        del(self.clients[nick])
        self.nicks.remove(nick)
        self.broadcast_nicklist()
        app_log.info("| CLIENT_REMOVED | %s" % nick)

    def set_instructor_state(self, state_txt):
        self._latest_instructor_state = state_txt

    def get_instructor_state(self):
        return self._latest_instructor_state 

    def generate_start_presentation_msg(self):
        params_obj = { 'source': '/static/presentations/pres1/index.html',
                        'follow_instructor': self._follow_instructor,
                        'lock_student_nav': self._lock_student_nav
        }
        msg_parts = ['load_presentation', json.dumps(params_obj) ]
        msg = '|'.join(msg_parts)
        return msg

    # Methods to send messages to the session clients

    def broadcast_nicklist(self):
        payload = '<ul>'
        for nick in self.nicks:
            payload += '<li>' + nick + '</li>'
        payload += '</ul>'
        msg = "nicklist|" + payload
        # loop through all clients and send msg
        for nick in self.clients:   # remember that the keys in self.clients are the nicks
            self.clients[nick]['connection'].write_message(msg)

    def send_start_presentation(self, connection):
        msg = self.generate_start_presentation_msg()
        connection.write_message(msg)

    def broadcast_start_presentation(self):
        msg = self.generate_start_presentation_msg()
        for nick in self.clients:   # remember that the keys in self.clients are the nicks
            self.clients[nick]['connection'].write_message(msg)
        self._presentation_started = True

    def broadcast_finish_presentation(self):
        msg_parts = ['finish_presentation']
        msg = '|'.join(msg_parts)
        for nick in self.clients:   # remember that the keys in self.clients are the nicks
            self.clients[nick]['connection'].write_message(msg)
        # reset session parameters NOT SURE HOW I'LL APPLY THIS
        self._presentation_started = False
        self._follow_instructor = True
        self._lock_student_nav = True

    def broadcast_reveal_state(self, state_txt):
        msg = 'instructor_state|' + state_txt
        for nick in self.clients:   # remember that the keys in self.clients are the nicks
            conn = self.clients[nick]['connection']
            if not conn._is_instructor:
                conn.write_message(msg)

    def broadcast_lock_student_nav(self, lockstate):
        msg = 'lock_student_nav|' + json.dumps(lockstate)
        for nick in self.clients:   # remember that the keys in self.clients are the nicks
            conn = self.clients[nick]['connection']
            if not conn._is_instructor:
                conn.write_message(msg)



class ClientWSConnection(websocket.WebSocketHandler):

    def initialize(self, session_controller):
        """Store a reference to the "external" SessionHandler instance"""
        self._SC = session_controller
        self._nick = None
        self._is_instructor = False

    def open(self):
        initial_nick = self.get_argument("nick")
        final_nick = self._SC.add_client(initial_nick, self)
        self._nick = final_nick
        app_log.info("| WS_OPENED | %s" % self._nick)
        # take advantage of the websocket to send html content
        # unusual thing to do, but very convenient in this app
        if (self._is_instructor):
            panel_content = render_file("instructor_panel.html")
            self.write_message('panel_content|' + panel_content)
        else:
            panel_content = render_file("participant_panel.html", { "navigation_locked": self._SC._lock_student_nav})
            self.write_message('panel_content|' + panel_content)
        # if the presentation has started when this connection joins
        # send the start presentation content to this client only
        if self._SC._presentation_started:
            self._SC.send_start_presentation(self)

    def on_close(self):
        self._SC.remove_client(self._nick)
        app_log.info("| WS_CLOSED | %s" % self._nick)

    def allow_draft76(self):
        # I think this was needed for Mac??
        return True

    def on_message(self, message):
        """Incoming request (message)  dispatcher."""

        app_log.info("| MSG_RECEIVED | %s" % (message,) )
        msg_parts = message.split('|')
        # use instrospection to call the right function for each message
        func_name = 'handle_' + msg_parts[0]
        func = getattr(self, func_name)
        if (func and callable(func)):
            func(msg_parts) 

    def set_instructor(self, is_instructor=False):
        self._is_instructor = is_instructor

    # request-handling methods

    def handle_start_presentation(self, msg_parts):
        # ignore this command if coming from a normal participant
        if not self._is_instructor:
            return
        self._SC.broadcast_start_presentation()

    def handle_slide_changed(self, msg_parts):
        # generate and send xapi statement, no matter who sends this message. For this we will have to json.loads(msg_body) to get some parameters.
        state_txt = msg_parts[1]
        if self._is_instructor:
            # store instructor state!
            self._SC.set_instructor_state(state_txt)
            if self._SC._follow_instructor:
                self._SC.broadcast_reveal_state(state_txt)

    def handle_lock_follow_instructor(self, msg_parts):
        # ignore this command if coming from a non-instructor connection
        if not self._is_instructor:
            return;
        lock = json.loads(msg_parts[1])
        self._SC._follow_instructor = lock

    def handle_lock_student_nav(self, msg_parts):
        # ignore this command if coming from a non-instructor connection
        if not self._is_instructor:
            return;
        lock = json.loads(msg_parts[1])
        self._SC._lock_student_nav = lock
        self._SC.broadcast_lock_student_nav(lock)

    def handle_sync_to_instructor(self, msg_parts):
        # this command works differently if it comes from a student or from the instructor
        state_txt = self._SC.get_instructor_state()
        if self._is_instructor:
            self._SC.broadcast_reveal_state(state_txt)
        else:  # the request came from a student, send just to this particular connectin
            msg = 'instructor_state|' + state_txt
            self.write_message(msg)

    def handle_finish_presentation(self, msg_parts):
        # ignore this command if coming from a non-instructor connection
        if not self._is_instructor:
            return;
        self._SC.broadcast_finish_presentation()


if __name__ == "__main__":

    tornado.options.define("port", default="8888", help="Port to listen on.")
    tornado.options.define("ip", default="", help="IP Address to listen on.")
    tornado.options.parse_command_line()

    sc = SessionController()
    enable_pretty_logging()
    app_log.level = logging.INFO

    settings = {
        "static_path": os.path.join(os.path.dirname(__file__), "static")
    }
    app = tornado.web.Application([
        (r"/", MainHandler, {'session_controller': sc}),
        (r"/ws", ClientWSConnection, {'session_controller': sc})
        ], **settings)

    app.listen(tornado.options.options.port,tornado.options.options.ip)
    app_log.info('Presentation System Server started.')
    app_log.info('listening on %s:%s...' % (tornado.options.options.ip,tornado.options.options.port))
    tornado.ioloop.IOLoop.instance().start()

