"""
   Copyright (C) 2021-2022 Katelynn Cadwallader.

   This file is part of Kuma Kuma Bear, a Discord Bot.

   Kuma Kuma Bear is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation; either version 3, or (at your option)
   any later version.

   Kuma Kuma Bear is distributed in the hope that it will be useful, but WITHOUT
   ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
   or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
   License for more details.

   You should have received a copy of the GNU General Public License
   along with Kuma Kuma Bear; see the file COPYING.  If not, write to the Free
   Software Foundation, 51 Franklin Street - Fifth Floor, Boston, MA
   02110-1301, USA. 

"""
from __future__ import annotations

import datetime
from datetime import timedelta, timezone
import enum
from importlib.resources import is_resource
from math import floor
import sqlite3
import discord
import os
import logging
from dataclasses import dataclass
from pprint import pprint
from pathlib import Path

from discord import Member, app_commands
from discord.app_commands import Choice
from discord.colour import Colour
from discord.enums import ButtonStyle
from discord.ext import commands, tasks

import pytz
import utils.asqlite as asqlite

from typing import Any, List, NamedTuple, Optional, Self, Union


import utils.timezones

script_loc: Path = Path(__file__).parent
DB_FILENAME = "lovers.sqlite"
DB_PATH: str = script_loc.joinpath(DB_FILENAME).as_posix()

LOVERS_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS lovers (
    name TEXT NOT NULL,
    discord_id BIGINT NOT NULL,
    role_switching INT NOT NULL DEFAULT 0,
    role INT NOT NULL,
    position_switching INT NOT NULL DEFAULT 0,
    position INT NOT NULL,
    PRIMARY KEY(discord_id)
)
"""

PARTNERS_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS partners (
    lovers_id INT NOT NULL,
    partner_id INT NOT NULL,
    role_switch INT NOT NULL,
    position_switching INT NOT NULL,
    s_time INT NOT NULL,
    FOREIGN KEY (lovers_id) references lovers(discord_id),
    FOREIGN KEY (partner_id) references lovers(discord_id)
    PRIMARY KEY(lovers_id, partner_id)
)
"""

KINKS_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS kinks (
    lovers_id INT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    FOREIGN KEY (lovers_id) references lovers(discord_id)
    PRIMARY KEY(lovers_id, name)
)
"""

TIMEZONE_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS user_settings (
    discord_id BIGINT NOT NULL,
    timezone TEXT NOT NULL,
    PRIMARY KEY(discord_id)
)"""

_logger = logging.getLogger()


class TimeTable:
    table: list[str] = []

    ante_post = ["AM", "PM"]
    hour_values = [str(x) for x in range(1, 13)]
    min_values = [str(x) for x in range(00, 60, 15)]

    def create_table(self):
        res = list(self.table)
        for entry in self.ante_post:
            for hour in self.hour_values:
                for min in self.min_values:
                    if min == "0":
                        min = "00"
                    res.append(f"{hour}:{min} {entry}")
        return res

    async def suggestion_time_diff(self, time: str, lover: LoverEntry) -> int:
        """Take's in a `H:MM AM` or `H:MM PM` format (respective of `lover` timezone) and returns the offset from UTC midnight in minutes."""
        # Let's parse our provided time str into int's.
        res = time.split(":")
        hour = int(res[0])
        min = int(res[1][0:-2])  # strip off "AM" or "PM"
        if res[1][-2:] == "PM":
            hour += 12
        # If we land on the hour; turn the "00" back into "0" minutes.
        if min == "00":
            min = 0
        lover_timezone = await lover.get_timezone()

        # We create our datetime object in the users local time zone using the values they want. (eg. 3:30am)
        today = datetime.date.today()
        s_time = datetime.time(hour=hour, minute=min)
        lover_cur_time_inTZ = pytz.timezone(lover_timezone["timezone"]).localize(datetime.datetime.combine(today, s_time))

        # We create a UTC datetime object at midnight
        utc_time = datetime.time(hour=0, minute=0)
        utc_midnight = pytz.timezone('UTC').localize(datetime.datetime.combine(today, utc_time))

        # We then get the difference between the two datetime objects into minutes as an offset from UTC Midnight.
        time_diff = lover_cur_time_inTZ - utc_midnight
        time_diff_minutes = time_diff.total_seconds() / 60
        if time_diff_minutes < 0:
            # add 24 hours
            time_diff_minutes += 60 * 24

        # Since UTC can be on a different DATE than the users Timezone,
        # we need to respectively remove 24hours or add 24hours.
        if time_diff_minutes > 1440:
            time_diff_minutes -= 1440
        elif time_diff_minutes < 0:
            time_diff_minutes = + 1440

        time_diff = timedelta(minutes=time_diff_minutes)

        return int(time_diff_minutes)

    async def localize_suggestion_time(self, suggestion_time: int, lover: LoverEntry) -> datetime.datetime:
        """Use the offset on UTC Midnight time and then convert that time to the lovers timezone"""
        lover_tz = await lover.get_timezone()
        today = datetime.date.today()
        hours = floor(suggestion_time / 60)
        minutes = (suggestion_time - (hours * 60))
        s_time = datetime.time(minute=minutes, hour=hours)
        utc_cur_time = pytz.timezone("UTC").localize(datetime.datetime.combine(today, s_time))
        return utc_cur_time.astimezone(tz=pytz.timezone(lover_tz["timezone"]))


class LoverEmbed(discord.Embed):
    @classmethod
    async def create(
        cls,
        *,
        color: int | Colour | None = None,
        title: Any | None = None,
        timestamp: datetime.datetime | None = None,
        lover: LoverEntry,
        interaction: discord.Interaction,
        guild: discord.Guild | None = None,
        member: discord.Member | discord.User | None = None,
    ):
        self = cls(color=color, title=title, timestamp=timestamp)
        if member is None:
            member = interaction.user

        self.set_thumbnail(url=None if member.avatar == None else member.avatar.url)
        # We are generating an embed via DM's we need to pass in a guild object prior for partner list generator.
        if guild is None:
            assert interaction.guild
            guild = interaction.guild

        # Generates the preferences for the Lover User under a single Embed Field
        lover_attrs: list[str] = [
            "role",
            "position",
            "position_switching",
            "role_switching",
        ]
        lover_preferences: list = []
        for entry in lover_attrs:
            if entry == "role_switching" or entry == "position_switching":
                lover_preferences.append(
                    f"- **{entry.title().replace('_', ' ')}**: {bool(getattr(lover, entry))}"
                )
            elif entry == "role":
                lover_preferences.append(
                    f"- **{entry.title()}**: {lover.get_role.title()}"
                )
            elif entry == "position":
                lover_preferences.append(
                    f"- **{entry.title()}**: {lover.get_position.title()}"
                )
        self.add_field(name="**__Preferences__**", value="\n".join(lover_preferences))

        # Partner Embed Field Generator
        partner_results: list = await lover.list_partners()
        if not len(partner_results):  # or partners is not None:
            self.add_field(
                name="**__Partners__**", value="*Currently no Partners*", inline=False
            )
        else:
            members: list[str] = [f"- **{member.display_name}**" for member in (guild.get_member(int(x)) for x in partner_results) if member]

            self.add_field(
                name="**__Partners__**",
                value="\n".join(members),
                inline=False,
            )

        # Kinks Embed Field Generator
        kink_results: list = await lover.list_kinks()
        if not len(kink_results):  # or kinks is not None:
            self.add_field(name="**__Kinks__**", value="*Currently no Kinks*")
        else:
            display_kinks: list = [f"- **{entry['name']}**" for entry in kink_results]
            self.add_field(
                name="**__Kinks__**", value="\n".join(display_kinks), inline=False
            )
        tz = await lover.get_timezone()
        if tz is not None:
            self.add_field(name="**__Timezone__**", value=tz["timezone"], inline=False)
        return self


class PartnerEmbed(discord.Embed):
    @classmethod
    async def create(
        cls,
        *,
        color: int | Colour | None = None,
        title: Any | None = None,
        timestamp: datetime.datetime | None = None,
        partner: discord.Member,
        lover_id: int
    ):

        self = cls(color=color, title=title, timestamp=timestamp)
        self.set_thumbnail(url=None if partner.avatar == None else partner.avatar.url)

        lover_partner: LoverEntry | None = await LoverEntry.get_or_none(discord_id=partner.id)
        lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=lover_id)
        assert lover

        if lover_partner is not None:
            lover_attrs: list[str] = [
                "role",
                "position",
                "position_switching",
                "role_switching",
            ]
            lover_preferences: list = []
            for entry in lover_attrs:
                if entry == "role_switching" or entry == "position_switching":
                    lover_preferences.append(
                        f"- **{entry.title().replace('_', ' ')}**: {bool(getattr(lover_partner, entry))}"
                    )
                elif entry == "role":
                    lover_preferences.append(
                        f"- **{entry.title()}**: {lover_partner.get_role.title()}"
                    )
                elif entry == "position":
                    lover_preferences.append(
                        f"- **{entry.title()}**: {lover_partner.get_position.title()}"
                    )
            self.add_field(name="**__Preferences__**", value="\n".join(lover_preferences), inline=False)

            # Suggestion time field
            res = await lover.get_partner_suggestion_time(partner_id=partner.id)
            lover_cur_time_inTZ = await TimeTable().localize_suggestion_time(suggestion_time=int(res[0]), lover=lover)
            self.add_field(name="** Suggestion Time **", value=lover_cur_time_inTZ.strftime("%I:%M %p"))

            # Kinks Embed Field Generator
            kink_results: list = await lover_partner.list_kinks()
            if not len(kink_results):  # or kinks is not None:
                self.add_field(name="**__Kinks__**", value="*Currently no Kinks*", inline=False)
            else:
                display_kinks: list = [f"- **{entry['name']}**" for entry in kink_results]
                self.add_field(
                    name="**__Kinks__**", value="\n".join(display_kinks), inline=False
                )
        return self


class LoverRoles(enum.Enum):
    dominant = 0
    submissive = 1


class LoverPositions(enum.Enum):
    top = 0
    bottom = 1


class LoverApproveButton(discord.ui.Button):
    def __init__(
        self,
        *,
        style: ButtonStyle = ButtonStyle.green,
        label: str = "Approve",
        custom_id: str = "approve_button"
    ):
        self.view: LoverPartnerView
        super().__init__(style=style, label=label, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        # return await super().callback(interaction)
        # Both Lover entries were validated prior; but just in case someone removes themselves we need to validate they still exist in the DB.
        # We only care if both are not none; because if one person "removes" themselves the partnership should fail.
        lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=self.view.sender.id)
        partner: LoverEntry | None = await LoverEntry.get_or_none(discord_id=self.view.maybe_partner.id)

        if lover is None:
            await self.view.maybe_partner.send(content=f"It appears {self.view.sender.display_name} is no longer a *Lover* member...")
            return await self.view.orig_msg.delete()

        elif partner is None:
            await self.view.sender.send(content=f"It appears {self.view.maybe_partner.display_name} is no longer a *Lover* member...")
            return await self.view.orig_msg.delete()

        # Add each other as partners
        if lover is not None and partner is not None:
            # Create our time offset
            cur_time = datetime.datetime.now()

            await lover.add_partner(
                partner_id=partner.discord_id,
                role_switching=lover.role_switching,
                position_switching=lover.position_switching,
                s_time=(cur_time.hour * 60 + cur_time.minute))
            await partner.add_partner(
                partner_id=lover.discord_id,
                role_switching=partner.role_switching,
                position_switching=partner.position_switching,
                s_time=(cur_time.hour * 60 + cur_time.minute))

        embed: LoverEmbed = await LoverEmbed.create(
            color=self.view.sender.color,
            title=self.view.sender.display_name,
            timestamp=discord.utils.utcnow(),
            lover=lover,
            interaction=interaction,
            guild=self.view.guild,
            member=self.view.sender,
        )
        await self.view.orig_msg.edit(
            content=f"You have approved **{self.view.sender.display_name}** to be your partner.",
            view=None,
            embed=embed)
        await self.view.sender.send(
            content=f"**{self.view.maybe_partner.display_name}** has __Approved__ your request to be their partner.")
        # self.view.res = True


class LoverDenyButton(discord.ui.Button):
    def __init__(
        self,
        *,
        style: ButtonStyle = ButtonStyle.red,
        label: str = "Deny",
        custom_id: str = "deny_button"
    ):
        self.view: LoverPartnerView
        super().__init__(style=style, label=label, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        # return await super().callback(interaction)
        await self.view.orig_msg.edit(content=f"You have denied **{self.view.sender.display_name}** to be your partner.", view=None)
        await self.view.sender.send(content=f"**{self.view.maybe_partner.display_name}** has __Denied__ your request to be their partner.")


class LoverPartnerView(discord.ui.View):
    @classmethod
    async def request(
        cls,
        *,
        sender: discord.Member | discord.User,
        maybe_partner: discord.Member | discord.User,
        guild: discord.Guild
    ):
        self = cls(timeout=None)
        cls.sender: discord.Member | discord.User = sender
        cls.guild: discord.Guild = guild
        cls.maybe_partner: Member | discord.User = maybe_partner
        self.add_item(LoverApproveButton(custom_id=f"approve_button.{maybe_partner.id}"))
        self.add_item(LoverDenyButton(custom_id=f"deny_button.{maybe_partner.id}"))
        # cls.res: bool = False
        cls.orig_msg = await maybe_partner.send(
            content=f"You have been requested to be a partner of {sender.mention}.",
            view=self)


async def get_range_suggestion_time(value1: int, value2: int):
    """ Selects all Partner rows where s_time is between `value1` and `value2` *is inclusive*"""
    async with asqlite.connect(DB_FILENAME) as db:
        async with db.cursor() as cur:
            await cur.execute("""SELECT lovers_id, partner_id FROM partners where s_time BETWEEN ? and ?""", value1, value2)
            res = await cur.fetchall()
            return res if not None else None


@dataclass(slots=True)
class LoverEntry:
    name: str
    discord_id: int

    role: int
    role_switching: bool

    position: int
    position_switching: bool

    @property
    def get_role(self) -> str:
        """Possible options see `LoverRoles`"""
        pos_roles = ["dominant", "submissive"]
        return pos_roles[self.role]

    @property
    def get_position(self) -> str:
        """Possible options see `LoverPositions`"""
        pos_position = ["top", "bottom"]
        return pos_position[self.position]

    # partners: list[dict[int, str]] #{id/owner_id : name}
    # kinks: list[dict[int, str]] #{id/owner_id: name}

    @classmethod
    async def get_or_none(cls, *, discord_id: int) -> LoverEntry | None:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute(
                    """SELECT * FROM lovers WHERE discord_id = ?""", discord_id
                )
                res = await cur.fetchone()

                return cls(**res) if res is not None else None

    @classmethod
    async def add_lover(
        cls,
        *,
        name: str,
        discord_id: int,
        role: int,
        position: int,
        role_switching: bool = False,
        position_switching: bool = False,
    ) -> LoverEntry | None:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute(
                    """INSERT INTO lovers(name, discord_id, role, role_switching, position, position_switching) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(discord_id) DO NOTHING RETURNING *""",
                    name,
                    discord_id,
                    role,
                    position,
                    role_switching,
                    position_switching,
                )
                res = await cur.fetchone()
                await db.commit()

                return cls(**res) if res is not None else None

    async def delete_lover(self) -> int:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                # remove from partner tables
                await cur.execute(
                    """DELETE FROM partners WHERE lovers_id = ?""", self.discord_id
                )
                await cur.execute(
                    """DELETE FROM kinks where lovers_id = ?""", self.discord_id
                )
                await cur.execute(
                    """DELETE FROM lovers WHERE discord_id = ?""", self.discord_id)
                await db.commit()

                return cur.get_cursor().rowcount

    # async def update_lover(self, name: str, role: int, position: int, role_switching: bool = False, position_switching: bool = False) -> LoverEntry:
    async def update_lover(self, args: dict[str, int | bool]) -> LoverEntry:
        SQL = []
        VALUES = []
        for entry in args:
            SQL.append(entry + " = ?")
            VALUES.append(args[entry])

        SQL = ", ".join(SQL)
        VALUES.append(self.discord_id)
       # print(SQL)
        # print(VALUES)
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                # await cur.execute("""UPDATE lovers SET name = ? WHERE discord_id = ? RETURNING *""", name, self.discord_id)
                await cur.execute(
                    f"""UPDATE lovers SET {SQL} WHERE discord_id = ? RETURNING *""",
                    tuple(VALUES),
                )
                await db.commit()

                res = await cur.fetchone()
                return LoverEntry(**res)

    async def add_partner(
        self,
        # partner_name: str,
        partner_id: int,
        # role: int,
        # position: int,
        role_switching: bool,
        position_switching: bool,
        s_time: int
    ) -> LoverEntry | None | bool:
        """Partners TABLE SCHEMA  
        ----------------------------

            lovers_id `INT NOT NULL`

            partner_id `INT NOT NULL` 

            role_switch `INT NOT NULL`

            position_switching `INT NOT NULL`

            s_time `INT NOT NULL`


        RETURNS
        -------------------------
        `False` - partner_id does not exist in `LOVERS` table \n
        `None` - partner_id/lover_id is already in the table as `PRIMARY KEY`.
        """

        partner: LoverEntry | None = await self.get_or_none(discord_id=partner_id)

        # if lover == None:
        #     lover = await self.add_lover(
        #         name=partner_name,
        #         discord_id=partner_id,
        #         role=role,
        #         position=position,
        #         role_switching=role_switching,
        #         position_switching=position_switching,
        #     )

        if partner is not None:
            async with asqlite.connect(DB_FILENAME) as db:
                async with db.cursor() as cur:
                    # await cur.execute("""INSERT INTO partners(lovers_id, partner_id) VALUES (?, ?)
                    # ON CONFLICT(lovers_id, partner_id) DO NOTHING RETURNING *""", lover.discord_id, partner_id)
                    try:
                        await cur.execute(
                            """INSERT INTO partners(lovers_id, partner_id, role_switch, position_switching, s_time) VALUES (?, ?, ?, ?, ?)""",
                            self.discord_id,
                            partner_id,
                            role_switching,
                            position_switching,
                            s_time
                            # lover.role_switching,
                            # lover.position_switching,
                        )
                        # await cur.execute("""INSERT INTO partners(partner_id) VALUES (?, ?))

                    except sqlite3.IntegrityError as err:
                        if (
                            type(err.args[0]) == str
                            and err.args[0].lower()
                            == "unique constraint failed: partners.lovers_id, partners.partner_id"
                        ):
                            return None

                    # res = await cur.fetchone()
                    await db.commit()
                    return partner
        else:
            return False
        # return lover

    async def remove_partner(self, partner_id: int) -> None | int:
        lover = await self.get_or_none(discord_id=partner_id)

        if lover == None:
            return lover

        else:
            async with asqlite.connect(DB_FILENAME) as db:
                async with db.cursor() as cur:
                    await cur.execute(
                        """DELETE FROM partners WHERE lovers_id = ? and partner_id = ?""",
                        self.discord_id,
                        partner_id,
                    )
                    await db.commit()

                    return cur.get_cursor().rowcount

    async def list_partners(self) -> list:
        """
        Returns a list of Discord IDs for lookup.
        """
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute(
                    """SELECT partner_id FROM partners WHERE lovers_id = ?""",
                    self.discord_id,
                )
                res = await cur.fetchall()
                # partners = []
                if res:
                    # for entry in res:
                    #     if entry["partner_id"] not in partners and entry["partner_id"] != self.discord_id:
                    #         partners.append(entry["partner_id"])
                    #     # if entry["lovers_id"] not in partners and entry["lovers_id"] != self.discord_id:
                    #     #     partners.append(entry["lovers_id"])
                    res = [entry["partner_id"] for entry in res]

                # return partners if len(partners) else None
                return res

    async def add_kink(
        self, name: str, description: Union[str, None] = None
    ) -> str | None:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute(
                    """INSERT INTO kinks(lovers_id, name, description) VALUES (?, ?, ?) 
                ON CONFLICT(lovers_id, name) DO NOTHING RETURNING *""",
                    self.discord_id,
                    name,
                    description,
                )
                res = await cur.fetchone()
                await db.commit()

                return name if res is not None else None

    async def remove_kink(self, name: str) -> int:
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute("""DELETE FROM kinks WHERE name = ?""", name)
                res = await cur.fetchone()
                await db.commit()

                return cur.get_cursor().rowcount

    async def update_partner(self, args: dict[str, int | bool | None]):
        """ Last value inside args must be  `partner_id`.\n
            PARTNER table layout

            lovers_id `INT NOT NULL`

            partner_id `INT NOT NULL` 

            role_switch `INT NOT NULL`

            position_switching `INT NOT NULL`

            s_time `INT NOT NULL`
        """
        SQL = []
        VALUES = []
        partner_id = 0
        for entry in args:
            # We don't need to set the partner_id; just need it for the WHERE statement.
            if entry == "partner_id":
                partner_id: int | bool | None = args[entry]
                continue
            SQL.append(entry + " = ?")
            VALUES.append(args[entry])

        SQL = ", ".join(SQL)
        VALUES.append(partner_id)
        VALUES.append(self.discord_id)
        # print("SQL", SQL)
        # print("VALUES", VALUES)
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute(
                    f"""UPDATE partners SET {SQL} WHERE partner_id = ? and lovers_id = ? RETURNING *""",
                    tuple(VALUES),
                )
                await db.commit()
                return

    # TODO Possibly bring this back and make a slash command for it. Unsure..
    # async def update_kink(self, name: str, new_name: str | None = None, new_description: str | None = None) -> int | None:
    #     async with asqlite.connect(DB_FILENAME) as db:
    #         async with db.cursor() as cur:
    #             await cur.execute("""SELECT * FROM kinks WHERE name = ?""", name)
    #             res = await cur.fetchone()
    #             if res is not None:
    #                 name = name if new_name == None else new_name
    #                 description = res["description"] if new_description == None else new_description
    #                 await cur.execute("""UPDATE kinks SET name = ?, description = ? WHERE name = ?""", name, description)

    async def list_kinks(self) -> list[Any]:
        """`RETURNS` list[Row("name" | "description" | "lover_id"]"""
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute(
                    """SELECT * FROM kinks WHERE lovers_id = ?""", self.discord_id
                )
                res = await cur.fetchall()

                return res if not None else None

    async def get_kink(self, name):
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute(""" SELECT * FROM kinks WHERE lovers_id =? and name = ?""", self.discord_id, name)
                res = await cur.fetchone()

                return res if not None else None

    async def set_timezone(self, tz: str):
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute(""" INSERT INTO user_settings(discord_id, timezone) VALUES($1, $2) 
                ON CONFLICT(discord_id) DO UPDATE SET timezone = $2""", self.discord_id, tz)
                res = await cur.fetchone()
                await db.commit()

                return res if not None else None

    async def get_timezone(self):
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute("""SELECT timezone from user_settings WHERE discord_id =?""", self.discord_id)
                res = await cur.fetchone()
                return res if not None else None

    async def get_partner_suggestion_time(self, partner_id: int):
        async with asqlite.connect(DB_FILENAME) as db:
            async with db.cursor() as cur:
                await cur.execute("""SELECT s_time FROM partners WHERE lovers_id = ? and partner_id = ?""", self.discord_id, partner_id)
                res = await cur.fetchone()
                return res if not None else None


class Love(commands.Cog):
    love_language = app_commands.Group(
        name="love", description="Love helper commands", nsfw=True
    )

    love_user = app_commands.Group(
        name="user",
        description="User profile commands",
        parent=love_language,
        nsfw=True,
        guild_only=True,
    )

    love_partner = app_commands.Group(
        name="partner",
        description="Partner related commands.",
        parent=love_language,
        nsfw=True,
        guild_only=True,
    )

    love_kinks = app_commands.Group(
        name="kinks",
        description="Kink related commands.",
        parent=love_language,
        nsfw=True,
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self._bot: commands.Bot = bot
        self._name: str = os.path.basename(__file__).title()
        self._logger = logging.getLogger()
        self._logger.info(f"**SUCCESS** Initializing {self._name} ")

        self._last_utc_minutes: int = (discord.utils.utcnow().hour * 60 + discord.utils.utcnow().minute)

    async def cog_load(self) -> None:
        # Generate our list of "Choices"
        self._timezones_choices: list[Choice[str]] = await utils.timezones.parse_bcp47_timezones()
        self._timezone_aliases: dict[str, str] = utils.timezones._timezone_aliases
        self._time_table = TimeTable()

        async with asqlite.connect(DB_FILENAME) as db:
            await db.execute(LOVERS_SETUP_SQL)
            await db.execute(PARTNERS_SETUP_SQL)
            await db.execute(KINKS_SETUP_SQL)
            await db.execute(TIMEZONE_SETUP_SQL)

        # await self.love_message_loop.start()

    async def cog_unload(self) -> None:
        if self.love_message_loop.is_running() is True:
            self.love_message_loop.cancel()

    def row_todict(
        self,
        lover: LoverEntry,
        row: list[sqlite3.Row] | None,
    ) -> dict[str, str] | None:
        """ Converts `list[Row]` into dict \n
        Any duplicate keys will append `(lover.name)` to the entry["name"].\n
        All values will contain `entry["name"]:lover.discord_id` for lookup.\n
        RETURNS = `{["name"] + f" ({lover.name})" : ["name"] + f":{lover.discord_id}"}`
        """
        # We will have two list[Row Factory] ideally; from different Lovers
        # There will be a chance of duplicate ["name"] values; so we should append and or add possible the Lover.name to the ["name"] value
        # We need to return the  "name=" ["name"] param of the Choice and "value=" "name" + "lover_id" aka Lovers(discord_id)
        res: dict[str, str] = {}
        if row is None:
            return None

        for values in row:
            res[values["name"]] = values["name"] + f":{lover.discord_id}"
        return res

    def merg_dict(
        self,
        dict1: dict[str, str],
        dict2: dict[str, str],
        lover: LoverEntry
    ) -> dict[str, str]:

        for entry in dict2:
            if entry in dict1:
                name = entry + f" ({lover.name})"
                dict1[name] = entry + f":{lover.discord_id}"
            else:
                dict1[entry] = dict2[entry]

        return dict1

    async def lover_handler(self, interaction: discord.Interaction, lover_id: int | str):
        if isinstance(lover_id, str):
            if len(lover_id) == 100:
                return await interaction.response.send_message(
                    content="You don't have any partners! Why did you select that option?",
                    ephemeral=True,
                )

            if not lover_id.isdigit():
                return await interaction.response.send_message(
                    content="You must choose from the options prompted to you.",
                    ephemeral=True,
                )
        lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=int(lover_id))
        if lover is None:
            return await interaction.response.send_message(
                content=f"It appears {interaction.user.display_name} is not a *Lover* user.",
                ephemeral=True
            )
        return lover

    async def partner_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice]:
        assert interaction.guild
        res: list[Choice] = [
            app_commands.Choice(name="No Entries Found...", value="x" * 100)
        ]
        choice_list: list[discord.Member] = []

        lover: LoverEntry | None = await LoverEntry.get_or_none(
            discord_id=interaction.user.id
        )
        if lover is not None:
            partners = await lover.list_partners()

            if partners is None:
                return res
            else:
                for id in partners:
                    # print(type(id["partner_id"]))
                    member: discord.Member | None = interaction.guild.get_member(id)
                    if member is not None:
                        # Choice[name= member.name, value= member.id]
                        choice_list.append(member)

            return [
                app_commands.Choice(name=member.display_name, value=str(member.id))
                for member in choice_list
                if current.lower() in member.name.lower()
            ]
        return res

    async def kinks_autocomplete(self, interaction: discord.Interaction, current: str):
        assert interaction.guild
        res: list[Choice] = [
            app_commands.Choice(name="No Entries Found...", value="x" * 100)
        ]
        choice_list: list[str] = []

        lover: LoverEntry | None = await LoverEntry.get_or_none(
            discord_id=interaction.user.id
        )
        if lover is not None:
            kinks = await lover.list_kinks()

            if kinks is None:
                return res
            else:
                for entry in kinks:
                    choice_list.append(entry["name"])

                return [
                    Choice(name=kink, value=kink)
                    for kink in choice_list
                    if current.lower() in kink.lower()
                ]
        return res

    # TODO Need to possible validate logic.
    async def partners_kinks_autocomplete(self, interaction: discord.Interaction, current: str):
        # Would like to possible know the kink name along with the description..
        assert interaction.guild
        kinks: dict[str, str] = {}
        res: list[Choice] = [
            app_commands.Choice(name="No Entries Found...", value="x" * 100)
        ]
        lover: LoverEntry | None = await LoverEntry.get_or_none(
            discord_id=interaction.user.id)

        if lover is None:
            return res

        lover_partners = await lover.list_partners()
        lover_kinks: dict[str, str] | None = self.row_todict(lover=lover, row=await lover.list_kinks() if not None else None)
        if lover_kinks is not None:
            kinks = self.merg_dict(dict1=kinks, dict2=lover_kinks, lover=lover)

        if len(lover_partners) and lover_partners is not None:
            # TODO - Turn this into list comp??
            # partners = [for partner   (await LoverEntry.get_or_none(discord_id=int(id)) for id in partners)]
            # partners: list[LoverEntry | None] = [await LoverEntry.get_or_none(discord_id=int(id)) for id in partner_res]
            for id in lover_partners:
                partner: LoverEntry | None = await LoverEntry.get_or_none(discord_id=int(id))
                if partner is None:
                    continue
                partners_kinks: dict[str, str] | None = self.row_todict(lover=partner, row=await partner.list_kinks() if not None else None)
                if partners_kinks is None:
                    continue
                else:
                    kinks = self.merg_dict(dict1=kinks, dict2=partners_kinks, lover=partner)

            return [Choice(name=key, value=value) for key, value in kinks.items() if current.lower() in key.lower()][:25]
        else:
            return res

    async def timezone_set_autocomplete(
            self, interaction: discord.Interaction,
            current: str) -> list[app_commands.Choice[str]]:
        cur_choices = list(self._timezones_choices)
        for key, value in self._timezone_aliases.items():
            if current.lower() in key.lower():
                cur_choices.append(app_commands.Choice(name=key, value=value))

        return [tz for tz in cur_choices if current.lower() in tz.name.lower()][:25]

        # if not argument:
        #     return timezones._default_timezones
        # matches: list[TimeZone] = timezones.find_timezones(argument)

    async def times_autocomplete(self, interaction: discord.Interaction, current: str):
        choice_list = [app_commands.Choice(name=x, value=x) for x in self._time_table.create_table()]
        return [time for time in choice_list if current.lower() in time.name.lower()][:25]

    # TODO - Figure out why this loop is blocking inside of `cog_load` and test DB lookup/results
    @tasks.loop(seconds=60)
    async def love_message_loop(self) -> None:
        # The goal of this loop is to look at all partner's suggested time (aka s_time) and use the minute value stored in the database
        # as a UTC offset from midnight against the current UTC time as for when to fire.
        cur_utc_minutes: int = (discord.utils.utcnow().hour * 60 + discord.utils.utcnow().minute)
        # res = await get_suggestion_time(value1=self.last_utc_minutes + 1, value2=cur_utc_minutes)
        res = await get_range_suggestion_time(value1=0, value2=9999)  # spoof test
        self.last_utc_minutes = cur_utc_minutes
        if res is not None:
            for partners in res:
                print(partners["partner_id"])
                print(partners["lovers_id"])
        print("finished loop")

    # @love_message_loop.before_loop
    # async def before_message_loop(self) -> None:
    #     await self._bot.wait_until_ready()

    # @tasks.loop(seconds=60)
    # async def love_dev_loop(self):
    #     print("Dev loop fired")
    #     time_format = '%x %I:%M %p'
    #     # My LoverEntry profile
    #     lover = await LoverEntry.get_or_none(discord_id=144462063920611328)
    #     assert lover
    #     time_test = await self._time_table.time_converter(time="6:56PM", lover=lover)

    #     today = datetime.date.today()
    #     utc_time = datetime.time(hour=0, minute=0)
    #     utc_midnight = pytz.timezone('UTC').localize(datetime.datetime.combine(today, utc_time))

    #     print("minutes", time_test)
    #     s_time = utc_midnight + timedelta(minutes=time_test)
    #     var1 = datetime.datetime.now(tz=pytz.timezone("UTC"))
    #     var2 = s_time
    #     print("UTC Now == TimeStamp", datetime.datetime.now(tz=pytz.timezone("UTC")).strftime(time_format), s_time.strftime(time_format))
    #     if var1 == var2 or var1 > var2:
    #         print("success")
    #     return

    # @love_language.command(name="reroll")
    # async def love_reroll(self, interaction: discord.Interaction):
    #     print()

    @love_user.command(name="add", description="Create your Lover profile.")
    @app_commands.describe(role="Your prefered `role` as a partner")
    @app_commands.describe(position="Your prefered `position` with partners.")
    async def love_user_add(
        self,
        interaction: discord.Interaction,
        # action: Choice[str],
        role: LoverRoles,
        position: LoverPositions,
        role_switching: bool = False,
        position_switching: bool = False
    ):

        lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=interaction.user.id)
        # if action.value == "add":
        if lover is None:
            await LoverEntry.add_lover(
                name=interaction.user.display_name,
                discord_id=interaction.user.id,
                role=role.value,
                position=position.value,
                role_switching=role_switching,
                position_switching=position_switching,
            )

            await interaction.response.send_message(
                content=f"Added *{interaction.user.display_name}*", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                content=f"You are already a *Lover* user, get out there!",
                ephemeral=True,
            )

    # TODO Verify Operation
    @love_user.command(name="delete", description="Remove your Lover profile.")
    async def love_user_delete(self, interaction: discord.Interaction):
        lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=interaction.user.id)
        if lover is not None:
            await lover.delete_lover()
            await interaction.response.send_message(
                content=f"We have removed {interaction.user.display_name} from the database, sad to see you go~",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                content=f"I was unable to find a Lover member by the name of `{interaction.user.display_name}`",
                ephemeral=True,
            )

    @love_user.command(name="update", description="Update your Lover profile.")
    @app_commands.autocomplete(tz=timezone_set_autocomplete)
    @app_commands.describe(tz="Set your timezone")
    async def love_user_update(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        role: LoverRoles | None = None,
        role_switching: bool | None = None,
        position: LoverPositions | None = None,
        position_switching: bool | None = None,
        tz: str | None = None
    ):
        func_args = locals()
        lover = await LoverEntry.get_or_none(discord_id=interaction.user.id)

        #    if action.value == "update":
        if lover is not None:
            # We only want to update vars that are not None
            func_args.pop("self")
            func_args.pop("interaction")
            func_args.pop("role")
            func_args.pop("position")
            func_args.pop("tz")
            # func_args.pop("lover")
            results: dict = {}

            # This is what is known as ooga booga 3 am code :D This is technically the most efficient way to do this because we don't remember any other way of doing this
            if role is not None:
                results["role"] = role.value
            if position is not None:
                results["position"] = position.value

            for entry in func_args:
                # if entry == "role" and role is not None:
                #     results["role"] = role.value
                # if entry == "position" and position is not None:
                #     results["position"] = position.value
                if func_args[entry] is not None:
                    results[entry] = func_args[entry]

            if len(results):
                await lover.update_lover(args=results)

            if tz is not None:
                try:
                    res = await utils.timezones.convert_timezones(tz=tz)
                except:
                    return await interaction.response.send_message(
                        content=f"You provided an improper Timezone; please pick from the provided selection only.",
                        ephemeral=True
                    )
                await lover.set_timezone(tz=tz)

            await interaction.response.send_message(
                content=f"We updated your *Lover* profile!", ephemeral=True
            )

    @love_user.command(name="info", description="Shows a Lovers profile information.")
    @app_commands.autocomplete(lover=partner_autocomplete)
    async def love_user_info(
        self, interaction: discord.Interaction, lover: str | None = None
    ):
        assert interaction.guild
        # This is for Partner lookup..
        if lover is not None:
            if len(lover) == 100:
                return await interaction.response.send_message(
                    content="You don't have any partners! Why did you select that option?",
                    ephemeral=True,
                )

            if not lover.isdigit():
                return await interaction.response.send_message(
                    content="You must choose from the options prompted to you.",
                    ephemeral=True,
                )

            res: LoverEntry | None = await LoverEntry.get_or_none(discord_id=int(lover))
            member: discord.Member | None = interaction.guild.get_member(int(lover))
            if res is not None and member is not None:
                return await interaction.response.send_message(
                    embed=await LoverEmbed.create(
                        color=member.color,
                        title=f"**{member.display_name}**",
                        timestamp=discord.utils.utcnow(),
                        lover=res,
                        interaction=interaction,
                        member=member,
                    ),
                    ephemeral=True,
                )

        # This is for self lookup...
        if lover is None:
            res = await LoverEntry.get_or_none(discord_id=interaction.user.id)
            if res is not None:
                return await interaction.response.send_message(
                    embed=await LoverEmbed.create(
                        color=interaction.user.color,
                        title=f"**{interaction.user.display_name}**",
                        timestamp=discord.utils.utcnow(),
                        lover=res,
                        interaction=interaction,
                    ),
                    ephemeral=True,
                )

            else:
                return await interaction.response.send_message(
                    content=f"**{interaction.user.display_name}** is not a Lover, ask them to add themselves first!",
                    ephemeral=True,
                )

    @love_user.command(name="timezone", description="Set your timezone for misc. bot interactions.")
    @app_commands.describe(tz="Set your timezone")
    @app_commands.autocomplete(tz=timezone_set_autocomplete)
    async def love_user_timezone(self, interaction: discord.Interaction, tz: str):
        lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=interaction.user.id)
        if lover is not None:
            # This will error out if someone provides a timezone that does not exist acting as a "hacky" validation.
            try:
                res = await utils.timezones.convert_timezones(tz=tz)
            except:
                return await interaction.response.send_message(
                    content=f"You provided an improper Timezone; please pick from the provided selection only.",
                    ephemeral=True
                )

            await lover.set_timezone(tz=tz)
            return await interaction.response.send_message(
                content=f"You timezone has been set to **{tz}** \n > Current date/time is: **{res.strftime('%x %I:%M %p')}**.",
                ephemeral=True)
        if lover is None:
            return await interaction.response.send_message(
                content=f"It looks like `{interaction.user.display_name}` is not a *lover* user.",
                ephemeral=True)

    @love_partner.command(name="add", description="Add a partner")
    async def love_partner_add(
        self,
        interaction: discord.Interaction,
        partner: discord.Member,
        # role_switching: bool | None = None,
        # position_switching: bool | None = None,
    ) -> None:
        assert interaction.guild

        if partner.id == interaction.user.id:
            return await interaction.response.send_message(
                content=f"You cannot add yourself as a partner... or can you?",  # cloning intensifies
                ephemeral=True)

        # Verify the possible partner is a Lover.
        partner_lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=partner.id)
        if partner_lover is None:
            return await interaction.response.send_message(
                content=f"**{partner.display_name}** is not a *Lover*, ask them to add themselves first!",
                ephemeral=True)

        # Verify the user is a Lover.
        lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=interaction.user.id)
        if lover is not None:
            # Lets verify the partner is not already a partner
            results = await lover.list_partners()
            if interaction.user.id in results:
                return await interaction.response.send_message(
                    content=f"Looks like **{partner.display_name}** is already your partner, get out there and have fun!",
                    ephemeral=True)
            else:
                await LoverPartnerView.request(
                    sender=interaction.user,
                    maybe_partner=partner,
                    guild=interaction.guild)
                return await interaction.response.send_message(
                    content=f"Lover request message sent to {partner.mention}",
                    ephemeral=True)

        # If the user is not a Lover; fail.
        if lover is None:
            return await interaction.response.send_message(
                content=f"It looks like `{interaction.user.display_name}` is not a *lover* user.",
                ephemeral=True)
        #     # Add the lover to the partner
        #     lover_partner = await LoverEntry.get_or_none(discord_id=partner.id)
        #     if lover_partner is not None:
        #         await lover_partner.add_partner(
        #             partner_id=interaction.user.id,
        #             role_switching=lover_partner.role_switching,
        #             position_switching=lover_partner.position_switching,
        #         )

        #     if type(lover_partner) == LoverEntry:
        #         await interaction.response.send_message(
        #             embed=await LoverEmbed.create(
        #                 color=partner.color,
        #                 title=partner.display_name,
        #                 timestamp=discord.utils.utcnow(),
        #                 lover=lover_partner,
        #                 interaction=interaction,
        #                 member=partner,
        #             ),
        #             ephemeral=True,
        #         )

    @love_partner.command(name="remove", description="Remove a partner.")
    @app_commands.autocomplete(partner=partner_autocomplete)
    async def love_partner_remove(
        self, interaction: discord.Interaction, partner: str
    ) -> None:
        if len(partner) == 100:
            return await interaction.response.send_message(
                content="You don't have any partners! Why did you select that option?",
                ephemeral=True,
            )

        if not partner.isdigit():
            return await interaction.response.send_message(
                content="You must choose from the options prompted to you.",
                ephemeral=True,
            )

        lover = await LoverEntry.get_or_none(discord_id=interaction.user.id)
        assert interaction.guild

        if lover is not None:
            res: int | None = await lover.remove_partner(partner_id=int(partner))
            partner_discord = interaction.guild.get_member(int(partner))
            await interaction.response.send_message(
                content=f"We removed your partner by the name of `{partner_discord.display_name}`."
                if partner_discord is not None
                else f"Unable to remove `{partner}`",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                content=f"It looks like `{interaction.user.display_name}` is not a *Lover* user.",
                ephemeral=True,
            )

    @love_partner.command(name="update", description="Update your relationship with a partner.")
    @app_commands.autocomplete(suggestion_time=times_autocomplete)
    @app_commands.autocomplete(partner=partner_autocomplete)
    async def love_partner_update(
        self,
        interaction: discord.Interaction,
        partner: str,
        role_switching: bool | None = None,
        position_switching: bool | None = None,
        suggestion_time: str | None = None
    ):

        func_args: dict[str, Any] = locals()
        func_args.pop("self")
        func_args.pop("interaction")
        func_args.pop("partner")
        func_args.pop("suggestion_time")

        s_time: float | None = None
        results: dict = {}

        for entry in func_args:
            if func_args[entry] is not None:
                results[entry] = func_args[entry]

        lover: LoverEntry | None = await self.lover_handler(interaction=interaction, lover_id=interaction.user.id)

        if lover is not None:
            if suggestion_time is not None:
                s_time = await self._time_table.suggestion_time_diff(time=suggestion_time, lover=lover)
                results["s_time"] = s_time
            if role_switching is None:
                role_switching = lover.role_switching
            if position_switching is None:
                position_switching = lover.position_switching
           # print("results", results)
            if len(results):
                # add our partners discord ID as our last value. We add the discord_id inside `update_partner`
                results["partner_id"] = int(partner)
                await lover.update_partner(args=results)
                pprint(results)
                # TODO Format reply displaying changed values as an embed?
                # for key, value in results:
                # example: {'partner_id': 479429344213860372, 's_time': 1680}
                await interaction.response.send_message(content=f"Updated user placeholder..")

    @love_partner.command(name="list", description="Lists all your partners")
    async def love_partner_list(self, interaction: discord.Interaction) -> None:
        lover = await LoverEntry.get_or_none(discord_id=interaction.user.id)
        assert interaction.guild

        if lover is None:
            return await interaction.response.send_message(
                content=f"It looks like `{interaction.user.display_name}` is not a *lover* user.",
                ephemeral=True,
            )

        res: list | None = await lover.list_partners()
        if res is not None and len(res):
            embeds: list[discord.Embed] = []
            rem_partner: int = 0

            for partner_id in res:
                partner: discord.Member | None = interaction.guild.get_member(int(partner_id))
                lover_partner: LoverEntry | None = await LoverEntry.get_or_none(discord_id=partner_id)

                # If we cannot find them at all (our DB or in the Guild)
                if partner is None and lover_partner is None:
                    await lover.remove_partner(partner_id=partner_id)
                    rem_partner += 1
                    await interaction.response.send_message(
                        content="We are unable to find one of your partners; removing them as your partner."
                    )
                    continue

                # if we cannot find them in the DB but they exists in the guild.
                if lover_partner is None and partner is not None:
                    await lover.remove_partner(partner_id=partner_id)
                    rem_partner += 1
                    await interaction.response.send_message(
                        content=f"It appears {partner.display_name} does not have a *Lover* profile anymore. Removing them as your partner.",
                        ephemeral=True)
                    continue

                # if we cannot find them in the guild but they are in the DB.
                if partner is None and lover_partner is not None:
                    await lover.remove_partner(partner_id=partner_id)
                    rem_partner += 1
                    await interaction.response.send_message(
                        content=f"{lover_partner.name} is no longer a member of this guild, removing them as your partner.",
                        ephemeral=True
                    )
                    continue

                if partner is not None and lover_partner is not None:
                    partner_embed: PartnerEmbed = await PartnerEmbed.create(
                        color=partner.color,
                        title=partner.display_name,
                        timestamp=discord.utils.utcnow(),
                        lover_id=interaction.user.id,
                        partner=partner
                    )
                    embeds.append(partner_embed)

            await interaction.response.send_message(
                content=f"**{interaction.user.display_name}**'s Partners {f'(Removed {rem_partner})' if rem_partner > 0 else ''}",
                embeds=embeds,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                content=f"You have no partners, consider adding some? Get out there and flirt sexy~<3",
                ephemeral=True,
            )

    @love_kinks.command(name="add", description="Add a type of Kink/Play you like.")
    async def love_kink_add(
        self,
        interaction: discord.Interaction,
        kink: str,
        description: Union[str, None] = None,
    ) -> None:
        lover = await LoverEntry.get_or_none(discord_id=interaction.user.id)
        if lover is not None:
            await lover.add_kink(name=kink, description=description)

            msg_content: str = f"Added `{kink}` to **{interaction.user.display_name}**"
            if description is not None:
                msg_content += "\n **Description**: " + description

            await interaction.response.send_message(content=msg_content, ephemeral=True)
        else:
            await interaction.response.send_message(
                content=f"It looks like `{interaction.user.display_name}` is not a *lover* user.",
                ephemeral=True,
            )

    @love_kinks.command(name="list", description="List all your Kinks.")
    @app_commands.autocomplete(lover=partner_autocomplete)
    async def love_kink_list(self, interaction: discord.Interaction, lover: str | None = None):
        assert interaction.guild
        res: LoverEntry | None = None
        member: discord.Member | None | discord.User = None
        # This is for Partner lookup..
        if lover is not None:
            if len(lover) == 100:
                return await interaction.response.send_message(
                    content="You don't have any partners! Why did you select that option?",
                    ephemeral=True,
                )

            if not lover.isdigit():
                return await interaction.response.send_message(
                    content="You must choose from the options prompted to you.",
                    ephemeral=True,
                )

            res = await LoverEntry.get_or_none(discord_id=int(lover))
            member = interaction.guild.get_member(int(lover))

        if lover is None:
            res: LoverEntry | None = await LoverEntry.get_or_none(discord_id=interaction.user.id)
            member = interaction.user

        if res is not None and member is not None:
            kinks = await res.list_kinks()
            if kinks is not None and len(kinks):
                kink_embed = discord.Embed(
                    title=f"{member.display_name} **Kinks~**",
                    color=member.color,
                    timestamp=discord.utils.utcnow())

                kink_embed.set_thumbnail(url=None if member.avatar == None else member.avatar.url)

                # Kinks Embed Field Generator
                kink_results: list = await res.list_kinks()
                if not len(kink_results):  # or kinks is not None:
                    kink_embed.add_field(name="**__Kinks__**", value="*Currently no Kinks*")
                else:
                    display_kinks: list = [f"- **{entry['name']}**" for entry in kink_results]
                    kink_embed.add_field(
                        name="**__Kinks__**", value="\n".join(display_kinks), inline=False
                    )
                await interaction.response.send_message(
                    embed=kink_embed,
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    content=f"It looks like you do not have any Kinks, consider adding some? *cracks whip*",
                    ephemeral=True,
                )
        if res is None:
            await interaction.response.send_message(
                content=f"It looks like the {member.display_name if member is not None else 'User'} specified is not a *Lover* user.",
                ephemeral=True,
            )
        if member is None:
            await interaction.response.send_message(
                content=f"It appears this {res.name if res is not None else 'User'} is no longer apart of the guild.",
                ephemeral=True
            )

    @love_kinks.command(name="remove", description="Remove a Kink.")
    @app_commands.autocomplete(kink=kinks_autocomplete)
    async def love_kink_remove(self, interaction: discord.Interaction, kink: str):
        lover = await LoverEntry.get_or_none(discord_id=interaction.user.id)
        if lover is not None:
            await lover.remove_kink(name=kink)
            await interaction.response.send_message(
                content=f"Aww no longer into `{kink}` anymore? If that changes just add it back.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                content=f"It looks like `{interaction.user.display_name}` is not a *lover* user.",
                ephemeral=True,
            )

    # TODO Need to validate logic and test for errors.
    @love_kinks.command(name="info", description="Look up the description of a Kink.")
    @app_commands.autocomplete(kink=partners_kinks_autocomplete)
    async def love_kink_info(self, interaction: discord.Interaction, kink: str):
        assert interaction.guild
        # convert our kink str into parts we need
        # should come in as `name:discord_id`
        kink_info: list = kink.split(":")
        kink_owner_id: int = int(kink_info[1])
        kink_name: str = kink_info[0]

        lover: LoverEntry | None = await LoverEntry.get_or_none(discord_id=kink_owner_id)
        if lover is None:
            return interaction.response.send_message(
                content=f"I was unable to find the owner of that kink..they appear to no longer be a *Lover*",
                ephemeral=True
            )
        member = interaction.guild.get_member(kink_owner_id)
        if member is None:
            return await interaction.response.send_message(
                content=f"Looks like {lover.name} is no longer apart of this server",
                ephemeral=True
            )

        if lover is not None:
            res = await lover.get_kink(name=kink_name)
            kink_description = res["description"]
            kink_embed = discord.Embed(title=f"**{lover.name}'s** Kink", color=member.color, timestamp=discord.utils.utcnow())
            kink_embed.set_thumbnail(url=None if member.avatar is None else member.avatar.url)
            kink_embed.add_field(name=f"**{kink_name}**", value=kink_description)

            await interaction.response.send_message(
                content=f"{kink.split(':')}",
                embed=kink_embed,
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Love(bot))
