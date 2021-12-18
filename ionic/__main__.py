import argparse
import asyncio
import datetime as dt
import random
import re
from asyncio.tasks import ALL_COMPLETED, FIRST_COMPLETED
from typing import Union

import discord as d
import jinja2
import quart
import sqlalchemy as sql
import uvloop
from arrow import Arrow
from dmux import DMux
from hypercorn.asyncio import serve
from hypercorn.config import Config
from jinja2.loaders import PackageLoader
from jinja2.utils import select_autoescape
from pytz import utc
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql.expression import delete, select
from sqlalchemy.sql.schema import Column
from sqlalchemy.sql.sqltypes import BigInteger, DateTime, String
from timefhuman import timefhuman

from . import cfg

Base = declarative_base()
db_engine = create_async_engine(cfg.db_url_async)
db_session = sessionmaker(db_engine, **cfg.db_session_kwargs)


config = Config()
config.bind = ["0.0.0.0:{}".format(cfg.port)]
j_env = jinja2.Environment(
    loader=PackageLoader("ionic"), autoescape=select_autoescape(), enable_async=True
)
j_template = j_env.get_template("time.jinja")
app = quart.Quart("ionic")

MESSAGE_DELETE_REACTION = "❌"
REGISTRATION_TIMEOUT = dt.timedelta(minutes=30)
# Regex discord elements
rgx_d_elems = re.compile("<(@!|#)[0-9]{18}>|<a{0,1}:[a-zA-Z0-9_.]{2,32}:[0-9]{18}>")
# Regex datetime markers
rgx_dt_markers = re.compile("<[^>][^>]+>")

shutdown_event = asyncio.Event()


def main():
    dmux = DMux()
    for server in cfg.server_list:
        ionic_server = IonicTraces(*server)
        dmux.register(ionic_server)

    try:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        asyncio.run(
            asyncio.wait(
                [
                    dmux.start(cfg.discord_token),
                    serve(
                        app,
                        config,
                    ),
                ],
                # When any of the tasks above exit, the whole app must exit
                # Having the registration server or dmux running alone
                # is of no use
                return_when=FIRST_COMPLETED,
            )
        )
    except asyncio.exceptions.CancelledError:
        # Ignore cancellation errors thrown on SIGTERM
        pass


class User(Base):
    __tablename__ = "mbd_user"
    __mapper_args__ = {"eager_defaults": True}
    id = Column("id", BigInteger, primary_key=True)
    tz = Column("tz", String)
    # Column used to mark a user for an update
    update_id = Column("update_id", BigInteger)
    update_dt = Column(
        "update_dt", DateTime(timezone=True), default=dt.datetime.now(tz=utc)
    )

    def __init__(self, id, tz):
        super().__init__()
        self.id = id
        self.tz = tz


@app.route("/<link_id>")
async def send_payload(link_id: int):
    payload = await j_template.render_async(response_url=cfg.app_url, link_id=link_id)
    return payload


@app.post("/")
async def receive_timezone():
    timezone = await quart.request.get_json()
    link_id = timezone["link_id"]
    timezone = timezone["tz"]

    async with db_session() as session:
        async with session.begin():
            user = (
                await session.execute(
                    select(User).where(User.update_id == int(link_id))
                )
            ).fetchone()
            if user is None:
                # If there is no such user, then no such user
                # has requested registration
                return
            user = user[0]
            if dt.datetime.now(tz=utc) - user.update_dt > REGISTRATION_TIMEOUT:
                return "Link timed out"
            user.tz = timezone
    return "Received"


class IonicTraces(DMux):
    def __init__(self, server_id: int, reg_channel_id: Union[int, None] = None):
        super().__init__()
        # The id of the server this instance is handling
        self.server_id = int(server_id)
        # The registration channel id for the server if one is provided
        self.reg_channel_id = (
            int(reg_channel_id) if reg_channel_id is not None else None
        )

    async def on_connect(self):
        try:
            server_name = (await self.client.fetch_guild(self.server_id)).name
        except d.errors.Forbidden:
            # The bot is not authorised to access this server
            print(
                "Ionic Trace connection failed for server id {}".format(self.server_id)
            )
        else:
            print(
                "Ionic Traces connected for server: {} id: {}".format(
                    server_name, self.server_id
                )
            )
            async with db_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

    async def on_message(self, message: d.Message):
        try:
            # Try access message.guild.id
            # This will fail with AttributeError if
            # the message isn't in a guild at all
            message.guild.id
        except AttributeError:
            pass
        else:
            if (
                message.guild.id == self.server_id
                and message.author != self.client.user
            ):
                # Only pass messages through if they're from the mbd server
                await asyncio.wait(
                    [
                        super().on_message(message),
                        self.registration_handler(message),
                        self.deregister_handler(message),
                        self.conversion_handler(message),
                    ],
                    return_when=ALL_COMPLETED,
                )
            if (
                message.guild.id == self.server_id
                and message.author.id == self.client.user.id
            ):
                # Respond only to bots own messages
                # This is to react to them with an emoji
                await message.add_reaction(MESSAGE_DELETE_REACTION)

    async def on_reaction_add(self, reaction: d.Reaction, user: d.User):
        # Do not react to servers we are not supposed to
        if reaction.message.guild.id != self.server_id:
            return

        # Do not respond to reactions on messages not sent by self
        if reaction.message.author.id != self.client.user.id:
            return

        # Do not respond to the specific reactions added by self
        # We can still respond to reactions that match ours,
        # but are added by others
        if user.id == self.client.user.id:
            return

        # Do not respond to emoji we don't care about
        if reaction.emoji != MESSAGE_DELETE_REACTION:
            return

        # If message_reacted to isn't sent by self,
        # ignore it
        message_reacted_to = reaction.message
        if message_reacted_to.author != self.client.user:
            return

        # If reaction is by user that did not trigger its creation
        # ignore it
        channel = reaction.message.channel
        message_replied_to = await channel.fetch_message(
            message_reacted_to.reference.message_id
        )
        time_author = message_replied_to.author
        if time_author.id != user.id:
            return

        # Delete the message if all checks have been passed
        await message_reacted_to.delete()

    async def conversion_handler(self, message: d.Message):
        # Pull properties we want from the message
        user_id = message.author.id
        content = message.content

        # Remove emoji, animated emoji, mentions, channels etc
        # from discord text
        content = rgx_d_elems.sub("", content)

        # Find time tokens
        time_list = rgx_dt_markers.findall(content)
        # Remove the angle brackets
        time_list = [time[1:-1] for time in time_list]
        # Ignore links
        time_list = [time for time in time_list if not time.startswith("http")]
        # Timefhuman always seems to throw a value error. Ignore these for now
        try:
            # Parse the human readable time to datetime format
            time_list = [timefhuman(time) for time in time_list]
        except ValueError:
            pass
        # Filter out items we don't understand
        time_list = [time for time in time_list if time != []]

        # If no times are specified/understood, skip the message
        if len(time_list) == 0:
            return

        # Find the user in the db
        async with db_session() as session:
            async with session.begin():
                user = (
                    await session.execute(select(User).where(User.id == user_id))
                ).fetchone()

        # If we can't find the user in the db, mention that they can register
        # or if their timezone record is empty
        if user is None or user[0].tz == "":
            if self.reg_channel_id is not None:
                await message.reply(
                    "You haven't registered with me yet or registration has failed\n"
                    + "Register by typing `?time` in the <#{}> channel".format(
                        self.reg_channel_id
                    )
                )
            else:
                await message.reply(
                    "You haven't registered with me yet or registration has failed\n"
                    + "Register by typing `?time.`"
                )
            return

        # Get the user's TimeZone
        tz = str(user[0].tz)

        # Account for time zones
        time_list = [Arrow.fromdatetime(time, tz) for time in time_list]
        # Convert to UTC
        utc_time_list = [time.to("UTC") for time in time_list]
        # Convert to unix time
        unix_time_list = [
            int((time - Arrow(1970, 1, 1)).total_seconds()) for time in utc_time_list
        ]
        # Create reply text
        reply = ":F>, <t:".join([str(time) for time in unix_time_list])
        reply = "<t:" + reply + ":F>"
        reply = "That's " + reply + " auto-converted to local time."
        await message.reply(reply)

    async def registration_handler(self, message: d.Message):
        if not (
            message.content == "?time"
            and (
                message.channel.id == self.reg_channel_id or self.reg_channel_id is None
            )
        ):
            return
        user_id = message.author.id
        link_id = random.randrange(1000000, 9999999, 1)

        # Add the link_id to the db
        async with db_session() as session:
            async with session.begin():
                instance = await session.get(User, int(user_id))
                if instance is None:
                    instance = User(int(user_id), "")
                instance.update_id = link_id
                session.add(instance)

        await message.author.send(
            "Visit this link to register your timezone: \n\n<{}{}>\n\n".format(
                cfg.app_url, link_id
            )
            + "This will collect and store your discord id and your timezone.\n"
            + "Both of these are only used to understand what time you mean when you use the bot. "
            + "This data is stored securely and not processed in any way and can be deleted with "
            + "`?time-deregister`"
        )
        await message.reply("Check your direct messages for a registration link")

    async def deregister_handler(self, message: d.Message):
        if not (
            message.content == "?time-deregister"
            and message.channel.id == self.reg_channel_id
        ):
            return
        # Find the user in the db
        async with db_session() as session:
            async with session.begin():
                # Delete the user's row
                await session.execute(delete(User).where(User.id == message.author.id))

        await message.reply("You have successfully unregistered")


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release", action="store_true", help="Performs release tasks for heroku"
    )
    parser.add_argument(
        "--reset", action="store_true", help="Deletes everything in the persistent db"
    )
    parser = parser.parse_args()

    if parser.release or parser.reset:
        if parser.reset:
            print("Deleting all tables")
            engine = sql.create_engine(cfg.db_url)
            meta = sql.MetaData()
            meta.reflect(bind=engine)
            for tbl in reversed(meta.sorted_tables):
                print("Dropping table", tbl)
                tbl.drop(engine)
            print("Remaining tables: ", len(sql.MetaData().sorted_tables), sep="")
        # Release tasks go here :
        # None as of now

    else:
        # If running an already deployed release, start the discord client
        main()
