import asyncio
import hashlib
import logging
import socket

from . import exceptions, stun
from .utils import random_transaction_id

logger = logging.getLogger('turn')


class TurnClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, server, username, password):
        self.lifetime = 600
        self.nonce = None
        self.password = password
        self.realm = None
        self.server = server
        self.transactions = {}
        self.username = username

    def connection_made(self, transport):
        logger.debug('%s connection_made(%s)', repr(self), transport)
        self.transport = transport

    async def channel_bind(self, channel_number, addr):
        request = stun.Message(message_method=stun.Method.CHANNEL_BIND,
                               message_class=stun.Class.REQUEST)
        request.attributes['CHANNEL-NUMBER'] = channel_number
        request.attributes['XOR-PEER-ADDRESS'] = addr
        self.__add_authentication(request)
        await self.request(request, self.server)
        logger.info('TURN channel bound %d %s' % (channel_number, repr(addr)))

    async def connect(self):
        """
        Create a TURN allocation.
        """
        request = stun.Message(message_method=stun.Method.ALLOCATE,
                               message_class=stun.Class.REQUEST)
        request.attributes['LIFETIME'] = self.lifetime
        request.attributes['REQUESTED-TRANSPORT'] = 0x11000000

        try:
            response = await self.request(request, self.server)
        except exceptions.TransactionFailed as e:
            response = e.response
            if response.attributes['ERROR-CODE'][0] == 401:
                # update long-term credentials
                self.nonce = response.attributes['NONCE']
                self.realm = response.attributes['REALM']
                self.integrity_key = hashlib.md5(
                    ':'.join([self.username, self.realm, self.password]).encode('utf8')).digest()

                # retry request with authentication
                request.transaction_id = random_transaction_id()
                self.__add_authentication(request)
                response = await self.request(request, self.server)

        relayed_address = response.attributes['XOR-RELAYED-ADDRESS']
        logger.info('TURN allocation created %s' % repr(relayed_address))
        return relayed_address

    async def close(self):
        """
        Releases the TURN allocation.
        """
        request = stun.Message(message_method=stun.Method.REFRESH,
                               message_class=stun.Class.REQUEST)
        request.attributes['LIFETIME'] = 0
        self.__add_authentication(request)
        await self.request(request, self.server)

        logger.info('TURN allocation released')

    async def refresh(self):
        """
        Refreshes the TURN allocation.
        """
        request = stun.Message(message_method=stun.Method.REFRESH,
                               message_class=stun.Class.REQUEST)
        request.attributes['LIFETIME'] = self.lifetime
        self.__add_authentication(request)
        await self.request(request, self.server)

    def datagram_received(self, data, addr):
        try:
            message = stun.parse_message(data)
            logger.debug('%s < %s %s', repr(self), addr, repr(message))
        except ValueError:
            return

        if ((message.message_class == stun.Class.RESPONSE or
             message.message_class == stun.Class.ERROR) and
           message.transaction_id in self.transactions):
            transaction = self.transactions[message.transaction_id]
            transaction.message_received(message, addr)

    async def request(self, request, addr):
        """
        Execute a STUN transaction and return the response.
        """
        assert request.transaction_id not in self.transactions

        transaction = stun.Transaction(request, addr, self)
        self.transactions[request.transaction_id] = transaction
        try:
            return await transaction.run()
        finally:
            del self.transactions[request.transaction_id]

    def send_stun(self, message, addr):
        """
        Send a STUN message.
        """
        logger.debug('%s > %s %s', repr(self), addr, repr(message))
        self.transport.sendto(bytes(message), addr)

    def __add_authentication(self, request):
        request.attributes['USERNAME'] = self.username
        request.attributes['NONCE'] = self.nonce
        request.attributes['REALM'] = self.realm
        request.add_message_integrity(self.integrity_key)
        request.add_fingerprint()

    def __repr__(self):
        return 'turn'


class TurnTransport:
    def __init__(self, protocol, inner_protocol):
        self.protocol = protocol
        self.__channels = {}
        self.__channel_number = 0x4000
        self.__inner_protocol = inner_protocol
        self.__relayed_address = None

    def close(self):
        asyncio.ensure_future(self.__inner_protocol.close())

    def get_extra_info(self, key):
        if key == 'relayed_address':
            return self.__relayed_address

    def sendto(self, data, addr):
        channel = self.__channels.get(addr)
        if channel is None:
            channel = self.__channel_number
            self.__channel_number += 1
            self.__channels[addr] = channel

            # bind channel
            asyncio.ensure_future(self.__inner_protocol.channel_bind(channel, addr))

    async def _connect(self):
        self.__relayed_address = await self.__inner_protocol.connect()
        self.protocol.connection_made(self)


async def create_turn_endpoint(protocol_factory, server_addr, username, password):
    """
    Create datagram connection relayed over TURN.
    """
    loop = asyncio.get_event_loop()
    _, inner_protocol = await loop.create_datagram_endpoint(
        lambda: TurnClientProtocol(server_addr,
                                   username=username,
                                   password=password),
        family=socket.AF_INET)

    protocol = protocol_factory()
    transport = TurnTransport(protocol, inner_protocol)
    await transport._connect()

    return transport, protocol
