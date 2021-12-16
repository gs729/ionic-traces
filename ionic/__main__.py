import argparse
import asyncio
import random
import re
from typing import Union

import discord as d
import jinja2
import quart
import sqlalchemy as sql
from arrow import Arrow
from dmux import DMux
from hypercorn.asyncio import serve
from hypercorn.config import Config
from jinja2.loaders import PackageLoader
from jinja2.utils import select_autoescape
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql.expression import delete, select
from sqlalchemy.sql.schema import Column
from sqlalchemy.sql.sqltypes import BigInteger, String
from timefhuman import timefhuman

from . import cfg

Base = declarative_base()
db_engine = create_async_engine(cfg.db_url_async)
db_session = sessionmaker(db_engine, **cfg.db_session_kwargs)


config = Config()
config.bind = ["0.0.0.0:{}".format(cfg.port)]
j_env = jinja2.Environment(
    loader=PackageLoader("ionic"), autoescape=select_autoescape()
)
j_template = j_env.get_template("time.jinja")
app = quart.Quart("ionic")

open_registration_list = {}


async def main():
    dmux = DMux()
    for server in cfg.server_list:
        ionic_server = IonicTraces(*server)
        dmux.register(ionic_server)
    await asyncio.gather(dmux.start(cfg.discord_token), serve(app, config))


class User(Base):
    __tablename__ = "mbd_user"
    __mapper_args__ = {"eager_defaults": True}
    id = Column("id", BigInteger, primary_key=True)
    tz = Column("tz", String)

    def __init__(self, id, tz):
        self.id = id
        self.tz = tz


@app.route("/<link_id>")
async def send_payload(link_id: int):
    payload = j_template.render(response_url=cfg.app_url, link_id=link_id)
    return payload


@app.post("/")
async def receive_timezone():
    timezone = await quart.request.get_json()
    link_id = timezone["link_id"]
    timezone = timezone["tz"]
    try:
        user_id = open_registration_list.pop(link_id)
    except KeyError:
        return "No Such Registration in Progress"

    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        async with session.begin():
            instance = await session.get(User, int(user_id))
            if instance is None:
                instance = User(int(user_id), str(timezone))
                session.add(instance)
            else:
                instance.tz = timezone

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
                await super().on_message(message)
                await self.registration_handler(message)
                await self.deregister_handler(message)
                await self.conversion_handler(message)

    async def conversion_handler(self, message: d.Message):
        # Pull properties we want from the message
        user_id = message.author.id
        content = message.content

        # Find time tokens
        time_list = re.findall("<[^>]+>", content)
        # Remove the angle brackets
        time_list = [time[1:-1] for time in time_list]
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
        if user is None:
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
        link_id = str(random.randrange(1000000, 9999999, 1))
        open_registration_list[link_id] = user_id

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
        asyncio.run(main())
