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
"""DiscoBot."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import re

from absl import logging
import asyncio
import discord
from typing import Optional, Text

from hypebot import types
from hypebot.core import params_lib
from hypebot.interfaces import interface_lib
from hypebot.protos.channel_pb2 import Channel

_COLOR_PATTERN = re.compile(
    r'\x03(?P<fg>\d\d)(?:|,(?P<bg>\d\d))(?P<txt>.*?)\x0f')


class DiscordInterface(interface_lib.BaseChatInterface):

  DEFAULT_PARAMS = params_lib.MergeParams(
      interface_lib.BaseChatInterface.DEFAULT_PARAMS,
      {
          'token': None,
      })

  def __init__(self, params):
    super(DiscordInterface, self).__init__(params)
    self._client = discord.Client()

    # TODO(someone): It's a bit odd defining these within __init__, but
    # otherwise we don't have access to client during method decoration time.
    @self._client.event
    async def on_message(message):
      # Discord doesn't protect us from responding to ourself.
      if message.author == self._client.user:
        return
      logging.info('Message from: %s - %s#%s - %s',
                   message.author.name, message.author.display_name,
                   message.author.discriminator, message.author.id)
      channel = Channel(
          id=message.channel.id,
          visibility=(Channel.PRIVATE if message.channel.is_private
                      else Channel.PUBLIC),
          name=message.channel.name)
      self._on_message_fn(channel, message.author.name,
                          self._CleanContent(message))

    @self._client.event
    async def on_ready():
      self.WhoAll()

    @self._client.event
    async def on_member_join(member):
      if member.bot:
        self._user_tracker.AddBot(member.name)
      else:
        self._user_tracker.AddHuman(member.name)

  def _CleanContent(self, message):
    """Transforms user/channel mentions into their names."""
    transformations = {
        re.escape('<#{0.id}>'.format(channel)): '#' + channel.name
        for channel in message.channel_mentions
    }
    transformations.update({
        re.escape('<@{0.id}>'.format(member)): member.name
        for member in message.mentions
    })

    def repl(obj):
      return transformations.get(re.escape(obj.group(0)), '')

    pattern = re.compile('|'.join(transformations.keys()))
    return pattern.sub(repl, message.content)

  def Loop(self):
    self._client.run(self._params.token)

  def Who(self, user: types.User):
    for server in self._client.servers:
      for member in server.members:
        if member.name == user:
          if member.bot:
            self._user_tracker.AddBot(user)
          else:
            self._user_tracker.AddHuman(user)

  def WhoAll(self):
    for server in self._client.servers:
      for member in server.members:
        if member.bot:
          self._user_tracker.AddBot(member.name)
        else:
          self._user_tracker.AddHuman(member.name)

  def _FindDiscordUser(self, user: Text) -> Optional[discord.Member]:
    """Find the corresponding user on any of the connected servers.

    Args:
      user: Nick of user.

    Returns:
      The corresponding discord member if they exist, otherwise None.
    """
    for server in self._client.servers:
      member = discord.utils.find(lambda m: m.name == user, server.members)
      if member:
        return member

  def Notice(self, channel: types.Channel, message: Text):
    pass

  def Topic(self, channel: types.Channel, new_topic: Text):
    self._client.loop.create_task(self._Topic(channel, new_topic))

  async def _Topic(self, channel: discord.Channel, new_topic: Text):
    disco_channel = self._client.get_channel(channel.id)
    if not disco_channel:
      logging.warning('Could not find corresponding Discord channel for %s',
                      channel)
      return
    self._client.edit_channel(disco_channel, topic=new_topic)

  def SendMessage(self, channel: types.Channel, message: types.Message):
    disco_channel = (self._client.get_channel(channel.id) or
                     self._FindDiscordUser(channel.id))
    if not disco_channel:
      return
    # Aggregate all lines into a single response to avoid Discord ratelimits.
    text_messages = []
    for msg in message.messages:
      # TODO(someone): Discord supports fancy messages.
      if msg.WhichOneof('message') == 'card':
        text_messages.extend(msg.card.alternate_text)
      else:
        text_messages.append(msg.text)
    self._client.loop.create_task(
        self._SendMessage(disco_channel, '\n'.join(text_messages)))

  async def _SendMessage(self, channel: discord.Channel, message: Text):
    message = _COLOR_PATTERN.sub(lambda obj: obj.groupdict()['txt'], message)
    await self._client.send_message(channel, message)
