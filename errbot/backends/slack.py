import json
import logging
import re
import time
import sys
from errbot import PY3
from errbot.backends import DeprecationBridgeIdentifier
from errbot.backends.base import (
    Message, build_message, Presence, ONLINE, AWAY,
    MUCRoom, RoomDoesNotExistError, UserDoesNotExistError
)
from errbot.errBot import ErrBot
from errbot.utils import deprecated

log = logging.getLogger(__name__)

try:
    from functools import lru_cache
except ImportError:
    from backports.functools_lru_cache import lru_cache
try:
    from slackclient import SlackClient
except ImportError:
    log.exception("Could not start the Slack back-end")
    log.fatal(
        "You need to install the slackclient package in order to use the Slack "
        "back-end. You should be able to install this package using: "
        "pip install slackclient"
    )
    sys.exit(1)
except SyntaxError:
    if not PY3:
        raise
    log.exception("Could not start the Slack back-end")
    log.fatal(
        "I cannot start the Slack back-end because I cannot import the SlackClient. "
        "Python 3 compatibility on SlackClient is still quite young, you may be "
        "running an old version or perhaps they released a version with a Python "
        "3 regression. As a last resort to fix this, you could try installing the "
        "latest master version from them using: "
        "pip install --upgrade https://github.com/slackhq/python-slackclient/archive/master.zip"
    )
    sys.exit(1)


# The Slack client automatically turns a channel name into a clickable
# link if you prefix it with a #. Other clients receive this link as a
# token matching this regex.
SLACK_CLIENT_CHANNEL_HYPERLINK = re.compile(r'^<#(?P<id>(C|G)[0-9A-Z]+)>$')


class SlackAPIResponseError(RuntimeError):
    """Slack API returned a non-OK response"""


class SlackIdentifier(DeprecationBridgeIdentifier):
    # TODO(gbin): remove this deprecation warnings at one point.

    def __init__(self, sc, userid, channelid=None):
        if userid[0] != 'U':
            raise Exception('This is not a Slack userid: %s' % userid)

        if channelid and channelid[0] != 'D' and channelid[0] != 'C':
            raise Exception('This is not a Slack channelid: %s' % channelid)

        self._userid = userid
        self._channelid = channelid
        self._sc = sc

    @property
    def userid(self):
        return self._userid

    @property
    def username(self):
        """Convert a Slack user ID to their user name"""
        user = self._sc.server.users.find(self._userid)
        if user is None:
            raise UserDoesNotExistError("Cannot find user with ID %s" % self._userid)
        return user.name

    @property
    def channelid(self):
        return self._channelid

    @property
    def channelname(self):
        """Convert a Slack channel ID to its channel name"""
        if self._channelid is None:
            return None

        channel = self._sc.server.channels.find(self._channelid)
        if channel is None:
            raise RoomDoesNotExistError("No channel with ID %s exists" % self._channelid)
        return channel.name

    @property
    def domain(self):
        return self._sc.server.domain

    # Compatibility with the generic API.
    person = username
    client = channelname

    def __unicode__(self):
        if self.channelname:
            return "%s@%s/%s" % (self.username, self.domain, self.channelname)
        else:
            return "%s@%s" % (self.username, self.domain)

    def __str__(self):
        return self.__unicode__()


class SlackMUCOccupant(SlackIdentifier):
    """
    This class represents a person inside a MUC.
    """
    room = SlackIdentifier.channelname


class SlackBackend(ErrBot):

    def __init__(self, config):
        super().__init__(config)
        identity = config.BOT_IDENTITY
        self.token = identity.get('token', None)
        if not self.token:
            log.fatal(
                'You need to set your token (found under "Bot Integration" on Slack) in '
                'the BOT_IDENTITY setting in your configuration. Without this token I '
                'cannot connect to Slack.'
            )
            sys.exit(1)
        self.sc = None  # Will be initialized in serve_once

    def api_call(self, method, data=None, raise_errors=True):
        """
        Make an API call to the Slack API and return response data.

        This is a thin wrapper around `SlackClient.server.api_call`.

        :param method:
            The API method to invoke (see https://api.slack.com/methods/).
        :param raise_errors:
            Whether to raise :class:`~SlackAPIResponseError` if the API
            returns an error
        :param data:
            A dictionary with data to pass along in the API request.
        :returns:
            The JSON-decoded API response
        :raises:
            :class:`~SlackAPIResponseError` if raise_errors is True and the
            API responds with `{"ok": false}`
        """
        if data is None:
            data = {}
        response = json.loads(self.sc.server.api_call(method, **data).decode('utf-8'))
        if raise_errors and not response['ok']:
            raise SlackAPIResponseError("Slack API call to %s failed: %s" % (method, response['error']))
        return response

    def serve_once(self):
        self.sc = SlackClient(self.token)
        log.info("Verifying authentication token")
        self.auth = self.api_call("auth.test", raise_errors=False)
        if not self.auth['ok']:
            raise SlackAPIResponseError("Couldn't authenticate with Slack. Server said: %s" % self.auth['error'])
        log.debug("Token accepted")
        self.bot_identifier = SlackIdentifier(self.sc, self.auth["user_id"])

        log.info("Connecting to Slack real-time-messaging API")
        if self.sc.rtm_connect():
            log.info("Connected")
            self.reset_reconnection_count()
            try:
                while True:
                    for message in self.sc.rtm_read():
                        if 'type' not in message:
                            log.debug("Ignoring non-event message: %s" % message)
                            continue

                        event_type = message['type']
                        event_handler = getattr(self, '_%s_event_handler' % event_type, None)
                        if event_handler is None:
                            log.debug("No event handler available for %s, ignoring this event" % event_type)
                            continue
                        try:
                            log.debug("Processing slack event: %s" % message)
                            event_handler(message)
                        except Exception:
                            log.exception("%s event handler raised an exception" % event_type)
                    time.sleep(1)
            except KeyboardInterrupt:
                log.info("Interrupt received, shutting down..")
                return True
            except:
                log.exception("Error reading from RTM stream:")
            finally:
                log.debug("Triggering disconnect callback")
                self.disconnect_callback()
        else:
            raise Exception('Connection failed, invalid token ?')

    def _hello_event_handler(self, event):
        """Event handler for the 'hello' event"""
        self.connect_callback()
        self.callback_presence(Presence(identifier=self.bot_identifier, status=ONLINE))

    def _presence_change_event_handler(self, event):
        """Event handler for the 'presence_change' event"""

        idd = SlackIdentifier(self.sc, event['user'])
        presence = event['presence']
        # According to https://api.slack.com/docs/presence, presence can
        # only be one of 'active' and 'away'
        if presence == 'active':
            status = ONLINE
        elif presence == 'away':
            status = AWAY
        else:
            log.error(
                "It appears the Slack API changed, I received an unknown presence type %s" % presence
            )
            status = ONLINE
        self.callback_presence(Presence(identifier=idd, status=status))

    def _message_event_handler(self, event):
        """Event handler for the 'message' event"""
        channel = event['channel']
        if channel.startswith('C'):
            log.debug("Handling message from a public channel")
            message_type = 'groupchat'
        elif channel.startswith('G'):
            log.debug("Handling message from a private group")
            message_type = 'groupchat'
        elif channel.startswith('D'):
            log.debug("Handling message from a user")
            message_type = 'chat'
        else:
            log.warning("Unknown message type! Unable to handle")
            return
        subtype = event.get('subtype', None)

        if subtype == "message_deleted":
            log.debug("Message of type message_deleted, ignoring this event")
            return
        if subtype == "message_changed" and 'attachments' in event['message']:
            # If you paste a link into Slack, it does a call-out to grab details
            # from it so it can display this in the chatroom. These show up as
            # message_changed events with an 'attachments' key in the embedded
            # message. We should completely ignore these events otherwise we
            # could end up processing bot commands twice (user issues a command
            # containing a link, it gets processed, then Slack triggers the
            # message_changed event and we end up processing it again as a new
            # message. This is not what we want).
            log.debug(
                "Ignoring message_changed event with attachments, likely caused "
                "by Slack auto-expanding a link"
            )
            return

        if 'message' in event:
            text = event['message']['text']
            user = event['message']['user']
        else:
            text = event['text']
            user = event['user']

        msg = Message(text, type_=message_type)
        if message_type == 'chat':
            msg.frm = SlackIdentifier(self.sc, user, event['channel'])
            msg.to = SlackIdentifier(self.sc, self.username_to_userid(self.sc.server.username),
                                     event['channel'])
        else:
            msg.frm = SlackMUCOccupant(self.sc, user, event['channel'])
            msg.to = SlackMUCOccupant(self.sc, self.username_to_userid(self.sc.server.username),
                                      event['channel'])

        msg.nick = msg.frm.userid
        self.callback_message(msg)

    def userid_to_username(self, id):
        """Convert a Slack user ID to their user name"""
        user = self.sc.server.users.find(id)
        if user is None:
            raise UserDoesNotExistError("Cannot find user with ID %s" % id)
        return user.name

    def username_to_userid(self, name):
        """Convert a Slack user name to their user ID"""
        user = self.sc.server.users.find(name)
        if user is None:
            raise UserDoesNotExistError("Cannot find user %s" % name)
        return user.id

    def channelid_to_channelname(self, id):
        """Convert a Slack channel ID to its channel name"""
        channel = self.sc.server.channels.find(id)
        if channel is None:
            raise RoomDoesNotExistError("No channel with ID %s exists" % id)
        return channel.name

    def channelname_to_channelid(self, name):
        """Convert a Slack channel name to its channel ID"""
        if name.startswith('#'):
            name = name[1:]
        channel = self.sc.server.channels.find(name)
        if channel is None:
            raise RoomDoesNotExistError("No channel named %s exists" % name)
        return channel.id

    def channels(self, exclude_archived=True, joined_only=False):
        """
        Get all channels and groups and return information about them.

        :param exclude_archived:
            Exclude archived channels/groups
        :param joined_only:
            Filter out channels the bot hasn't joined
        :returns:
            A list of channel (https://api.slack.com/types/channel)
            and group (https://api.slack.com/types/group) types.

        See also:
          * https://api.slack.com/methods/channels.list
          * https://api.slack.com/methods/groups.list
        """
        response = self.api_call('channels.list', data={'exclude_archived': exclude_archived})
        channels = [channel for channel in response['channels']
                    if channel['is_member'] or not joined_only]

        response = self.api_call('groups.list', data={'exclude_archived': exclude_archived})
        # No need to filter for 'is_member' in this next call (it doesn't
        # (even exist) because leaving a group means you have to get invited
        # back again by somebody else.
        groups = [group for group in response['groups']]

        return channels + groups

    @lru_cache(50)
    def get_im_channel(self, id):
        """Open a direct message channel to a user"""
        response = self.api_call('im.open', data={'user': id})
        return response['channel']['id']

    def send_message(self, mess):
        super().send_message(mess)
        to_humanreadable = "<unknown>"
        try:
            if mess.type == 'groupchat':
                to_humanreadable = mess.to.username
                to_channel_id = mess.to.channelid
            else:
                to_humanreadable = mess.to.username
                to_channel_id = mess.to.channelid
            log.debug('Sending %s message to %s (%s)' % (mess.type, to_humanreadable, to_channel_id))
            self.sc.rtm_send_message(to_channel_id, mess.body)
        except Exception:
            log.exception(
                "An exception occurred while trying to send the following message "
                "to %s: %s" % (to_humanreadable, mess.body)
            )

    def build_message(self, text):
        return build_message(text, Message)

    def build_identifier(self, txtrep):
        """ username@domainname/channelname
            or username/channelname
            or username
            domain is actually pointless as it will be self.sc.server.domain
            """
        if '@' in txtrep:
            username, domain_channel = txtrep.split('@')
            if '/' in domain_channel:
                domain, channelname = domain_channel.split('/')
            else:
                domainname = domain_channel
                channel = None
        else:
            if '/' in txtrep:
                username, channelname = txtrep.split('/')
            else:
                username = txtrep

        userid = self.username_to_userid(username)
        if channelname:
            channelid = self.channelname_to_channelid(channelname)
        else:
            channelid = None

        return SlackIdentifier(self.sc, userid, channelid)

    def build_reply(self, mess, text=None, private=False):
        msg_type = mess.type
        response = self.build_message(text)

        response.frm = self.bot_identifier
        response.to = mess.frm
        response.type = 'chat' if private else msg_type

        return response

    def is_admin(self, usr):
        return usr.split('@')[0] in self.bot_config.BOT_ADMINS

    def shutdown(self):
        super().shutdown()

    @deprecated
    def join_room(self, room, username=None, password=None):
        return self.query_room(room)

    @property
    def mode(self):
        return 'slack'

    def query_room(self, room):
        """ Room can either be a name or a channelid """
        if room.startswith('C') or room.startswith('G'):
            return SlackRoom(channelid=room, bot=self)

        m = SLACK_CLIENT_CHANNEL_HYPERLINK.match(room)
        if m is not None:
            return SlackRoom(channelid=m.groupdict()['id'], bot=self)

        return SlackRoom(name=room, bot=self)

    def rooms(self):
        """
        Return a list of rooms the bot is currently in.

        :returns:
            A list of :class:`~SlackRoom` instances.
        """
        channels = self.channels(joined_only=True, exclude_archived=True)
        return [SlackRoom(channelid=channel['id'], bot=self) for channel in channels]

    def groupchat_reply_format(self):
        return '@{0}: {1}'


class SlackRoom(MUCRoom):
    def __init__(self, name=None, channelid=None, bot=None):
        if channelid != '' and name is not None:
            raise ValueError("channelid and name are mutually exclusive")

        if name is not None:
            if name.startswith('#'):
                self._name = name[1:]
            else:
                self._name = name
        else:
            self._name = bot.channelid_to_channelname(channelid)

        self._id = None
        self.sc = bot.sc

    def __str__(self):
        return "#%s" % self.name

    @property
    def _channel(self):
        """
        The channel object exposed by SlackClient
        """
        id = self.sc.server.channels.find(self.name)
        if id is None:
            raise RoomDoesNotExistError(
                "%s does not exist (or is a private group you don't have access to)" % str(self)
            )
        return id

    @property
    def _channel_info(self):
        """
        Channel info as returned by the Slack API.

        See also:
          * https://api.slack.com/methods/channels.list
          * https://api.slack.com/methods/groups.list
        """
        if self.private:
            return self._bot.api_call('groups.info', data={'channel': self.id})["group"]
        else:
            return self._bot.api_call('channels.info', data={'channel': self.id})["channel"]

    @property
    def private(self):
        """Return True if the room is a private group"""
        return self._channel.id.startswith('G')

    @property
    def id(self):
        """Return the ID of this room"""
        if self._id is None:
            self._id = self._channel.id
        return self._id

    @property
    def name(self):
        """Return the name of this room"""
        return self._name

    def join(self, username=None, password=None):
        log.info("Joining channel %s" % str(self))
        self._bot.api_call('channels.join', data={'name': self.name})

    def leave(self, reason=None):
        if self.id.startswith('C'):
            log.info("Leaving channel %s (%s)" % (str(self), self.id))
            self._bot.api_call('channels.leave', data={'channel': self.id})
        else:
            log.info("Leaving group %s (%s)" % (str(self), self.id))
            self._bot.api_call('groups.leave', data={'channel': self.id})
        self._id = None

    def create(self, private=False):
        if private:
            log.info("Creating group %s" % str(self))
            self._bot.api_call('groups.create', data={'name': self.name})
        else:
            log.info("Creating channel %s" % str(self))
            self._bot.api_call('channels.create', data={'name': self.name})

    def destroy(self):
        if self.id.startswith('C'):
            log.info("Archiving channel %s (%s)" % (str(self), self.id))
            self._bot.api_call('channels.archive', data={'channel': self.id})
        else:
            log.info("Archiving group %s (%s)" % (str(self), self.id))
            self._bot.api_call('groups.archive', data={'channel': self.id})
        self._id = None

    @property
    def exists(self):
        channels = self._bot.channels(joined_only=False, exclude_archived=False)
        return len([c for c in channels if c['name'] == self.name]) > 0

    @property
    def joined(self):
        channels = self._bot.channels(joined_only=True)
        return len([c for c in channels if c['name'] == self.name]) > 0

    @property
    def topic(self):
        if self._channel_info['topic']['value'] == '':
            return None
        else:
            return self._channel_info['topic']['value']

    @topic.setter
    def topic(self, topic):
        if self.private:
            log.info("Setting topic of %s (%s) to '%s'" % (str(self), self.id, topic))
            self._bot.api_call('groups.setTopic', data={'channel': self.id, 'topic': topic})
        else:
            log.info("Setting topic of %s (%s) to '%s'" % (str(self), self.id, topic))
            self._bot.api_call('channels.setTopic', data={'channel': self.id, 'topic': topic})

    @property
    def purpose(self):
        if self._channel_info['purpose']['value'] == '':
            return None
        else:
            return self._channel_info['purpose']['value']

    @purpose.setter
    def purpose(self, purpose):
        if self.private:
            log.info("Setting purpose of %s (%s) to '%s'" % (str(self), self.id, purpose))
            self._bot.api_call('groups.setPurpose', data={'channel': self.id, 'purpose': purpose})
        else:
            log.info("Setting purpose of %s (%s) to '%s'" % (str(self), self.id, purpose))
            self._bot.api_call('channels.setPurpose', data={'channel': self.id, 'purpose': purpose})

    @property
    def occupants(self):
        members = self._channel_info['members']
        return [SlackMUCOccupant(
                node=self.name,
                domain=self._bot.sc.server.domain,
                resource=self._bot.userid_to_username(m))
                for m in members]

    def invite(self, *args):
        users = {user['name']: user['id'] for user in self._bot.api_call('users.list')['members']}
        for user in args:
            if user not in users:
                raise UserDoesNotExistError("User '%s' not found" % user)
            log.info("Inviting %s into %s (%s)" % (user, str(self), self.id))
            method = 'groups.invite' if self.private else 'channels.invite'
            response = self._bot.api_call(
                method,
                data={'channel': self.id, 'user': users[user]},
                raise_errors=False
            )
            if not response['ok'] and response['error'] != "already_in_channel":
                raise SlackAPIResponseError("Slack API call to %s failed: %s" % (method, response['error']))
