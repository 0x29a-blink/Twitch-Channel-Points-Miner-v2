import json
import logging
import random
import time
from threading import Thread  # , Timer

from TwitchChannelPointsMiner.classes.entities.Message import Message
from TwitchChannelPointsMiner.classes.entities.Raid import Raid
from TwitchChannelPointsMiner.classes.Settings import Events, Settings
from TwitchChannelPointsMiner.classes.TwitchWebSocket import TwitchWebSocket
from TwitchChannelPointsMiner.constants import WEBSOCKET
from TwitchChannelPointsMiner.utils import (
    get_streamer_index,
    internet_connection_available,
)

logger = logging.getLogger(__name__)


class WebSocketsPool:
    __slots__ = ["ws", "twitch", "streamers"]

    def __init__(self, twitch, streamers):
        self.ws = []
        self.twitch = twitch
        self.streamers = streamers

    """
    API Limits
    - Clients can listen to up to 50 topics per connection. Trying to listen to more topics will result in an error message.
    - We recommend that a single client IP address establishes no more than 10 simultaneous connections.
    The two limits above are likely to be relaxed for approved third-party applications, as we start to better understand third-party requirements.
    """

    def submit(self, topic):
        # Check if we need to create a new WebSocket instance
        if self.ws == [] or len(self.ws[-1].topics) >= 50:
            self.ws.append(self.__new(len(self.ws)))
            self.__start(-1)

        self.__submit(-1, topic)

    def __submit(self, index, topic):
        # Topic in topics should never happen. Anyway prevent any types of duplicates
        if topic not in self.ws[index].topics:
            self.ws[index].topics.append(topic)

        if self.ws[index].is_opened is False:
            self.ws[index].pending_topics.append(topic)
        else:
            self.ws[index].listen(
                topic, self.twitch.twitch_login.get_auth_token())

    def __new(self, index):
        return TwitchWebSocket(
            index=index,
            parent_pool=self,
            url=WEBSOCKET,
            on_message=WebSocketsPool.on_message,
            on_open=WebSocketsPool.on_open,
            on_error=WebSocketsPool.on_error,
            on_close=WebSocketsPool.on_close
            # on_close=WebSocketsPool.handle_reconnection, # Do nothing.
        )

    def __start(self, index):
        if Settings.disable_ssl_cert_verification is True:
            import ssl
            thread_ws = Thread(target=lambda: self.ws[index].run_forever(
                sslopt={"cert_reqs": ssl.CERT_NONE}))
            logger.warn("SSL certificate verification is disabled! Be aware!")
        else:
            thread_ws = Thread(target=lambda: self.ws[index].run_forever())
        thread_ws.daemon = True
        thread_ws.name = f"WebSocket #{self.ws[index].index}"
        thread_ws.start()

    def end(self):
        for index in range(0, len(self.ws)):
            self.ws[index].forced_close = True
            self.ws[index].close()

    @staticmethod
    def on_open(ws):
        def run():
            ws.is_opened = True
            ws.ping()

            for topic in ws.pending_topics:
                ws.listen(topic, ws.twitch.twitch_login.get_auth_token())

            while ws.is_closed is False:
                # Else: the ws is currently in reconnecting phase, you can't do ping or other operation.
                # Probably this ws will be closed very soon with ws.is_closed = True
                if ws.is_reconneting is False:
                    ws.ping()  # We need ping for keep the connection alive
                    time.sleep(random.uniform(25, 30))

                    if ws.elapsed_last_pong() > 5:
                        logger.info(
                            f"#{ws.index} - The last PONG was received more than 5 minutes ago"
                        )
                        WebSocketsPool.handle_reconnection(ws)

        thread_ws = Thread(target=run)
        thread_ws.daemon = True
        thread_ws.start()

    @staticmethod
    def on_error(ws, error):
        # Connection lost | [WinError 10054] An existing connection was forcibly closed by the remote host
        # Connection already closed | Connection is already closed (raise WebSocketConnectionClosedException)
        logger.error(f"#{ws.index} - WebSocket error: {error}")

    @staticmethod
    def on_close(ws, close_status_code, close_reason):
        logger.info(f"#{ws.index} - WebSocket closed")
        # On close please reconnect automatically
        WebSocketsPool.handle_reconnection(ws)

    @staticmethod
    def handle_reconnection(ws):
        # Close the current WebSocket.
        ws.is_closed = True
        ws.keep_running = False
        # Reconnect only if ws.forced_close is False (replace the keep_running)

        # Set the current socket as reconnecting status
        # So the external ping check will be locked
        ws.is_reconneting = True

        if ws.forced_close is False:
            logger.info(
                f"#{ws.index} - Reconnecting to Twitch PubSub server in ~60 seconds"
            )
            time.sleep(30)

            while internet_connection_available() is False:
                random_sleep = random.randint(1, 3)
                logger.warning(
                    f"#{ws.index} - No internet connection available! Retry after {random_sleep}m"
                )
                time.sleep(random_sleep * 60)

            # Why not create a new ws on the same array index? Let's try.
            self = ws.parent_pool
            # Create a new connection.
            self.ws[ws.index] = self.__new(ws.index)

            self.__start(ws.index)  # Start a new thread.
            time.sleep(30)

            for topic in ws.topics:
                self.__submit(ws.index, topic)

    @staticmethod
    def on_message(ws, message):
        logger.debug(f"#{ws.index} - Received: {message.strip()}")
        response = json.loads(message)

        if response["type"] == "MESSAGE":
            # We should create a Message class ...
            message = Message(response["data"])

            # If we have more than one PubSub connection, messages may be duplicated
            # Check the concatenation between message_type.top.channel_id
            if (
                ws.last_message_type_channel is not None
                and ws.last_message_timestamp is not None
                and ws.last_message_timestamp == message.timestamp
                and ws.last_message_type_channel == message.identifier
            ):
                return

            ws.last_message_timestamp = message.timestamp
            ws.last_message_type_channel = message.identifier

            streamer_index = get_streamer_index(
                ws.streamers, message.channel_id)
            if streamer_index != -1:
                try:
                    if message.topic == "community-points-user-v1":
                        if message.type in ["points-earned", "points-spent"]:
                            balance = message.data["balance"]["balance"]
                            ws.streamers[streamer_index].channel_points = balance
                            # Analytics switch
                            if Settings.enable_analytics is True:
                                ws.streamers[streamer_index].persistent_series(
                                    event_type=message.data["point_gain"]["reason_code"]
                                    if message.type == "points-earned"
                                    else "Spent"
                                )

                        if message.type == "points-earned":
                            earned = message.data["point_gain"]["total_points"]
                            reason_code = message.data["point_gain"]["reason_code"]

                            logger.info(
                                f"+{earned} → {ws.streamers[streamer_index]} - Reason: {reason_code}.",
                                extra={
                                    "emoji": ":rocket:",
                                    "event": Events.get(f"GAIN_FOR_{reason_code}"),
                                },
                            )
                            ws.streamers[streamer_index].update_history(
                                reason_code, earned
                            )
                            # Analytics switch
                            if Settings.enable_analytics is True:
                                ws.streamers[streamer_index].persistent_annotations(
                                    reason_code, f"+{earned} - {reason_code}"
                                )
                        elif message.type == "claim-available":
                            ws.twitch.claim_bonus(
                                ws.streamers[streamer_index],
                                message.data["claim"]["id"],
                            )

                    elif message.topic == "video-playback-by-id":
                        # There is stream-up message type, but it's sent earlier than the API updates
                        if message.type == "stream-up":
                            ws.streamers[streamer_index].stream_up = time.time()
                        elif message.type == "stream-down":
                            if ws.streamers[streamer_index].is_online is True:
                                ws.streamers[streamer_index].set_offline()
                        elif message.type == "viewcount":
                            if ws.streamers[streamer_index].stream_up_elapsed():
                                ws.twitch.check_streamer_online(
                                    ws.streamers[streamer_index]
                                )

                    elif message.topic == "raid":
                        if message.type == "raid_update_v2":
                            raid = Raid(
                                message.message["raid"]["id"],
                                message.message["raid"]["target_login"],
                            )
                            ws.twitch.update_raid(
                                ws.streamers[streamer_index], raid)
                except Exception:
                    logger.error(
                        f"Exception raised for topic: {message.topic} and message: {message}",
                        exc_info=True,
                    )

        elif response["type"] == "RESPONSE" and len(response.get("error", "")) > 0:
            raise RuntimeError(
                f"Error while trying to listen for a topic: {response}")

        elif response["type"] == "RECONNECT":
            logger.info(f"#{ws.index} - Reconnection required")
            WebSocketsPool.handle_reconnection(ws)

        elif response["type"] == "PONG":
            ws.last_pong = time.time()
