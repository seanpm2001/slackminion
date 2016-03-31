import logging

from datetime import datetime
from slackclient import SlackClient
from time import sleep

from dispatcher import MessageDispatcher
from plugin import PluginManager
from slack import SlackEvent, SlackChannel, SlackUser
from webserver import Webserver


class NotSetupError(Exception):
    def __str__(self):
        return "Bot not setup.  Please run start() before run()."


def eventhandler(*args, **kwargs):
    def wrapper(func):
        if isinstance(kwargs['events'], basestring):
            kwargs['events'] = [kwargs['events']]
        func.is_eventhandler = True
        func.events = kwargs['events']
        return func
    return wrapper


class Bot(object):
    def __init__(self, config, test_mode=False):
        self.always_send_dm = []
        self.config = config
        self.dispatcher = MessageDispatcher()
        self.event_handlers = {}
        self.is_setup = False
        self.log = logging.getLogger(__name__)
        self.plugins = PluginManager(self, test_mode)
        self.runnable = True
        self.sc = None
        self.webserver = None
        self.test_mode = test_mode

        if self.test_mode:
            self.metrics = {
                'startup_time': 0
            }

    def start(self):
        if self.test_mode:
            bot_start_time = datetime.now()
        self.plugins.load()
        self.plugins.load_state()
        self._find_event_handlers()
        self.sc = SlackClient(self.config['slack_token'])
        self.webserver = Webserver(self.config['webserver']['host'], self.config['webserver']['port'])

        self.always_send_dm = ['_unauthorized_']
        if 'always_send_dm' in self.config:
            self.always_send_dm.extend(map(lambda x: '!' + x, self.config['always_send_dm']))

        # Rocket is very noisy at debug
        logging.getLogger('Rocket.Errors.ThreadPool').setLevel(logging.INFO)

        self.is_setup = True
        if self.test_mode:
            self.metrics['startup_time'] = (datetime.now() - bot_start_time).total_seconds() * 1000.0

    def _find_event_handlers(self):
        for name, method in self.__class__.__dict__.iteritems():
            if hasattr(method, 'is_eventhandler'):
                for event in method.events:
                    self.event_handlers[event] = method

    def run(self):

        # Fail out if setup wasn't run
        if not self.is_setup:
            raise NotSetupError

        if not self.sc.rtm_connect():
            return False

        # Start the web server
        self.webserver.start()
        try:
            while self.runnable:
                # Get all waiting events - this always returns a list
                events = self.sc.rtm_read()
                for e in events:
                    self._handle_event(e)
                sleep(0.1)
        except KeyboardInterrupt:
            # On ctrl-c, just exit
            pass
        except:
            self.log.exception('Unhandled exception')

    def stop(self):
        if self.webserver is not None:
            self.webserver.stop()
        if not self.test_mode:
            self.plugins.save_state()

    def send_message(self, channel, text):
        # This doesn't want the # in the channel name
        if isinstance(channel, SlackChannel):
            channel = channel.channelid
        self.log.debug("Trying to send to %s: %s", channel, text)
        self.sc.rtm_send_message(channel, text)

    def send_im(self, user, text):
        if isinstance(user, SlackUser):
            user = user.userid
        channelid = self._find_im_channel(user)
        self.send_message(channelid, text)

    def _find_im_channel(self, user):
        resp = self.sc.api_call('im.list')
        channels = filter(lambda x: x['user'] == user, resp['ims'])
        if len(channels) > 0:
            return channels[0]['id']
        resp = self.sc.api_call('im.open', user=user)
        return resp['channel']['id']

    def _load_user_rights(self, user):
        if 'bot_admins' in self.config:
            if user.username in self.config['bot_admins']:
                user.is_admin = True

    def _handle_event(self, event):
        if 'type' not in event:
            # This is likely a notification that the bot was mentioned
            self.log.debug("Received odd event: %s", event)
            return
        e = SlackEvent(sc=self.sc, **event)
        self.log.debug("Received event type: %s", e.type)
        if e.type in self.event_handlers:
            self.event_handlers[e.type](self, e)

    @eventhandler(events='message')
    def _event_message(self, msg):
        self.log.debug("Message.message: %s: %s: %s", msg.channel, msg.user, msg.__dict__)
        self._load_user_rights(msg.user)
        try:
            cmd, output = self.dispatcher.push(msg)
        except:
            self.log.exception('Unhandled exception')
            return
        self.log.debug("Output from dispatcher: %s", output)
        if output:
            if cmd in self.always_send_dm:
                self.send_im(msg.user, output)
            else:
                self.send_message(msg.channel, output)
