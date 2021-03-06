# Copyright 2018 The Hypebot Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The core of all things hype."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import itertools
from threading import Lock
import time

from absl import logging
from concurrent import futures
from typing import Any, Callable, Dict, List, Optional, Text, Union

from hypebot import types
from hypebot.core import async_lib
from hypebot.core import proxy_lib
from hypebot.core import schedule_lib
from hypebot.core import util_lib
from hypebot.core import zombie_lib
from hypebot.interfaces import interface_lib
from hypebot.plugins import coin_lib
from hypebot.plugins import deploy_lib
from hypebot.plugins import hypestack_lib
from hypebot.plugins import inventory_lib
from hypebot.protos.channel_pb2 import Channel
from hypebot.stocks import stock_factory
from hypebot.storage import storage_factory


# TODO(someone): Remove and replace usage with direct dependency on types lib.
_MessageType = Union[
    Text,
    List[Text]]
MessageType = Optional[_MessageType]


def _MakeMessage(response: _MessageType) -> types.Message:
  msg = types.Message()
  _AppendToMessage(msg, response)
  return msg


def _GetAlternateTextList(value: Union[Text, List[Text]]) -> List[Text]:
  if isinstance(value, Text):
    return value.split('\n')
  # Flat map to expand newlines to separate list items.
  return list(itertools.chain.from_iterable([x.split('\n') for x in value]))


def _AppendToMessage(msg: types.Message, response: _MessageType):
  if isinstance(response, (bytes, Text)):
    for line in response.split('\n'):
      msg.messages.add(text=line)
  else:
    assert isinstance(response, list)
    for line in response:
      _AppendToMessage(msg, line)


class RequestTracker(object):
  """Tracks user requests that require confirmation."""

  _REQUEST_TIMEOUT_SEC = 60

  def __init__(self, reply_fn: Callable) -> None:
    self._reply_fn = reply_fn
    self._pending_requests = {}  # type: Dict[types.User, Dict]
    self._pending_requests_lock = Lock()

  def HasPendingRequest(self, user: types.User) -> bool:
    with self._pending_requests_lock:
      return user in self._pending_requests

  def RequestConfirmation(self,
                          user: types.User,
                          summary: str,
                          request_details: Dict,
                          action_fn: Callable,
                          parse_fn: Optional[Callable] = None) -> None:
    """Create a user request that must be confirmed before action is taken.

    This is a very generic flow useful for any command/bot service that would
    like to double-check with the user before some action is taken (e.g. filing
    an issue). There can be only a single pending request per user at a time.
    When there is an outstanding request for user, all other calls to this
    function will fail until either the user confirms or denies their pending
    request, or _REQUEST_TIMEOUT_SEC has elapsed.

    Args:
      user: The user making the request.
      summary: Summary of the request, used in confirmation message.
      request_details: Information passed to action_fn upon confirmation.
      action_fn: Function called if user confirms this request.
      parse_fn: Function used to parse a user's response.

    Returns:
      None
    """
    now = time.time()
    with self._pending_requests_lock:
      previous_request = self._pending_requests.get(user, None)
      if previous_request:
        if now - previous_request['timestamp'] < self._REQUEST_TIMEOUT_SEC:
          self._reply_fn(user,
                         'Confirm prior request before submitting another.')
          return
        del self._pending_requests[user]

      request_details['timestamp'] = now
      request_details['action'] = action_fn
      if not parse_fn:
        parse_fn = lambda x: x.lower().startswith('y')
      request_details['parse'] = parse_fn
      self._pending_requests[user] = request_details
      self._reply_fn(user, 'Confirm %s?' % summary)

  def ResolveRequest(self, user: types.User, user_msg: str) -> None:
    """Resolves a pending request, taking the linked action if confirmed."""
    now = time.time()
    with self._pending_requests_lock:
      request_details = self._pending_requests.get(user)
      if not request_details:
        return
      if not request_details['parse'](user_msg):
        self._reply_fn(user, 'Cancelling request.')
      elif now - request_details['timestamp'] >= self._REQUEST_TIMEOUT_SEC:
        self._reply_fn(user, 'You took too long to confirm, try again.')
      else:
        self._reply_fn(user,
                       request_details.get('action_text',
                                           'Confirmation accepted.'))
        request_details['action'](user, request_details)
      del self._pending_requests[user]


class OutputUtil(object):
  """Allows plugins to send output without a reference to Core."""

  def __init__(self, output_fn: Callable) -> None:
    self._output_fn = output_fn

  def LogAndOutput(self,
                   log_level: int,
                   channel: Channel,
                   message: MessageType) -> None:
    """Logs message at log_level, then sends it to channel via Output."""
    logging.log(log_level, message)
    self.Output(channel, message)

  def Output(self, channel: Channel, message: MessageType) -> None:
    """Outputs a message to channel."""
    self._output_fn(channel, message)


class Core(object):
  """The core of hypebot.

  Any state or service that is needed by more than one command.
  """

  def __init__(
      self,
      params: Any,  # HypeParams
      interface: interface_lib.BaseChatInterface) -> None:
    """Constructs core of hypebot.

    Args:
      params: Bot parameters.
      interface: This will always be the original interface that the bot was
        created with, and never the CaptureInterface during nested calls. For
        this reason, you should only call Join/Part and potentially Notice/Topic
        on this interface.  Don't call SendMessage or else it can send messages
        never intended for human consumption.
      hypeletter_callback: brcooley get rid of this when migrating hypeletter to
          its own command.
    """
    self.params = params
    self.nick = self.params.name.lower()
    self.interface = interface
    self.output_util = OutputUtil(self.Reply)

    self.store = storage_factory.CreateFromParams(self.params.storage)
    cached_type = self.params.storage.get(self.params.storage.type, {}).get(
        'cached_type')
    if cached_type:
      self.cached_store = storage_factory.Create(
          cached_type, self.params.storage.get(self.params.storage.type))
    else:
      logging.info('No cached_type found for storage, using default store.')
      self.cached_store = self.store

    self.user_tracker = util_lib.UserTracker()
    self.timezone = self.params.time_zone
    self.scheduler = schedule_lib.HypeScheduler(self.timezone)
    self.executor = futures.ThreadPoolExecutor(max_workers=8)
    self.runner = async_lib.AsyncRunner(self.executor)
    self.inventory = inventory_lib.InventoryManager(self.store)
    self.proxy = proxy_lib.Proxy(self.store)
    self.zombie_manager = zombie_lib.ZombieManager(self.Reply)
    self.request_tracker = RequestTracker(self.Reply)
    self.bank = coin_lib.Bank(self.store, self.nick)
    self.bets = coin_lib.Bookie(self.store, self.bank, self.inventory)
    self.stocks = stock_factory.CreateFromParams(self.params.stocks, self.proxy)
    self.deployment_manager = deploy_lib.DeploymentManager(
        self.nick, self.bets, self.output_util, self.executor)
    self.hypestacks = hypestack_lib.HypeStacks(self.store, self.bank,
                                               self.Reply)
    self.betting_games = []
    self.last_command = None
    self.default_channel = Channel(visibility=Channel.PUBLIC,
                                   **self.params.default_channel.AsDict())

  def Reply(self,
            channel: types.Target,
            msg: MessageType,
            default_channel: Optional[Channel] = None,
            limit_lines: bool = False,
            max_public_lines: int = 6,
            user: Optional[types.User] = None,
            log: bool = False,
            log_level: int = logging.INFO) -> None:
    """Sends a message to the channel.

    Leaving Reply on the HypeCore allows replacing the interface to process
    nested commands. However, some change will be needed in order to actually
    create an OutputUtil for HBDS without a HypeCore.

    Args:
      channel: Who/where to send the message.
      msg: The message to send.
      default_channel: Who/where to send the message if no channel is specified.
      limit_lines: Whether to limit lines or not.
      max_public_lines: Maximum number of lines to send to a public channel.
      user: If specified, where to send the message if its too long.
      log: Whether to also log the message.
      log_level: How important the log is.
    """
    if not msg:
      return

    if log:
      text_msg = msg
      logging.log(log_level, text_msg, exc_info=log_level == logging.ERROR)

    channel = channel or default_channel
    if not channel:
      logging.info('Attempted to send message with no channel: %s', msg)
      return
    # Support legacy Reply to users as a string.
    if not isinstance(channel, Channel):
      # Send messages for sub-accounts to the real user.
      channel = Channel(id=channel.split(':')[0],
                        visibility=Channel.PRIVATE,
                        name=channel)

    if (limit_lines and channel.visibility == Channel.PUBLIC and
        isinstance(msg, list) and len(msg) > max_public_lines):
      if user:
        self.interface.SendMessage(
            channel, _MakeMessage('It\'s long so I sent it privately.'))
        self.interface.SendMessage(
            Channel(id=user, visibility=Channel.PRIVATE, name=user),
            _MakeMessage(msg))
      else:
        # If there is no user, just truncate and send to channel.
        self.interface.SendMessage(
            channel, _MakeMessage(msg[:max_public_lines] + ['...']))
    else:
      self.interface.SendMessage(channel, _MakeMessage(msg))

  def ReloadData(self) -> bool:
    """Asynchronous reload of all data on core.

    Searches for any attribute that has a ReloadData function and calls it.

    Returns:
      Whether reload triggered or not since it was still running.
    """
    if not self.runner.IsIdle():
      logging.info('Runner not idle, can not trigger reload.')
      return False

    self.proxy.FlushCache()
    for obj in self.__dict__.values():
      if hasattr(obj, 'ReloadData'):
        logging.info('Triggering reload for: %s', obj.__class__.__name__)
        self.runner.RunAsync(obj.ReloadData)
    return True
