# Reddit Image Scraper cog
from datetime import datetime, timezone
from configparser import ConfigParser
import io
from pathlib import Path
from PIL import Image as IMG
from PIL.Image import Image
import discord
import pytz
import json
import hashlib
import sys
import tzlocal
import time
import aiohttp
from aiohttp import ClientResponse
from typing import TYPE_CHECKING, TypedDict, Union, Literal, reveal_type
from typing_extensions import Any
from fake_useragent import UserAgent
import asyncpraw
from asyncpraw.models import Subreddit
import utils.asqlite as asqlite
from sqlite3 import Row
from discord.ext import commands, tasks
from discord import app_commands

from utils import cog
from utils import ImageComp


script_loc: Path = Path(__file__).parent
DB_FILENAME = "reddit_scrape.sqlite"
DB_PATH: str = script_loc.joinpath(DB_FILENAME).as_posix()

SUBREDDIT_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS subreddit (
    id INTEGER PRIMARY KEY NOT NULL,
    name TEXT COLLATE NOCASE NOT NULL UNIQUE,
    webhook_id INTEGER,
    FOREIGN KEY (webhook_id) references webhook(id)
)"""

WEBHOOK_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS webhook (
    id INTEGER PRIMARY KEY NOT NULL,
    name TEXT COLLATE NOCASE NOT NULL UNIQUE,
    url TEXT NOT NULL UNIQUE
)"""


class Webhook(TypedDict):
    name: str
    url: str
    id: int


async def _get_subreddit(name: str) -> Row | None:
    """
    Get a Row from the Subreddit Table.

    Args:
        name (str): Name of Subreddit

    Returns:
        Row['id', 'name', 'webhook_id'] | None
    """
    async with asqlite.connect(DB_PATH) as db:
        async with db.cursor() as cur:
            await cur.execute("""SELECT id, name, webhook_id FROM subreddit where name = ?""", name)
            res: Row | None = await cur.fetchone()
            await cur.close()
            return res if not None else None


async def _add_subreddit(name: str) -> Row | None:
    """
    Add a Row to the Subreddit Table.

    Args:
        name (str): Name of Subreddit

    Returns:
        Row['id','name','webhook_id'] | None
    """
    res = await _get_subreddit(name=name)
    if res is not None:
        return None
    else:
        async with asqlite.connect(DB_PATH) as db:
            async with db.cursor() as cur:
                await cur.execute("""INSERT INTO subreddit(name) VALUES(?) ON CONFLICT(name) DO NOTHING RETURNING *""", name)
                await db.commit()
                res: Row | None = await cur.fetchone()
                await cur.close()
                return res if not None else None


async def _del_subreddit(name: str) -> int | None:
    """
    Delete a Row from the Subreddit Table

    Args:
        name (str): Name of Subreddit

    Returns:
        int | None: Row count
    """
    res: Row | None = await _get_subreddit(name=name)
    if res is None:
        return None
    else:
        async with asqlite.connect(DB_PATH) as db:
            async with db.cursor() as cur:
                await cur.execute("""DELETE FROM subreddit WHERE name = ?""", name)
                res = await cur.fetchone()
                await db.commit()
                count: int = cur.get_cursor().rowcount
                await cur.close()
                return count
                # return res if not None else None


async def _get_all_subreddits(webhooks: bool = True) -> list[str | Union[Any, str, dict[str, str | None]]]:
    """
    Gets all Row entries of the Subreddit Table. Typically including the webhook IDs.

    Args:
        webooks(bool): Set to False to not include webhook IDs when getting subreddits. Defaults True.
    Returns:
        list[Union[Any, dict[str, str]]]: An empty list if no entries in the Subreddit table.  

        Otherwise a list of dictionaries structured as `[{"subreddit", "webhook url"}]`
    """
    _subreddits: list[dict[str, Union[str, None]]] = []
    async with asqlite.connect(DB_PATH) as db:
        async with db.cursor() as cur:
            await cur.execute("""SELECT name, webhook_id FROM subreddit""")
            res = await cur.fetchall()
            if res is None or len(res) == 0:
                return []

            if webhooks == False:
                _subs: list[str] = [entry["name"] for entry in res]
                return _subs
            else:
                for entry in res:
                    res_webhook = await _get_webhook(arg=entry["webhook_id"])
                    if res_webhook is not None:
                        res_webhook = res_webhook["url"]
                    _subreddits.append({entry["name"]: res_webhook})

        return _subreddits


async def _update_subreddit(name: str, webhook: Union[int, str]) -> Row | None:
    """
    Update a Subreddit Row `webhook_id` value.

    Args:
        name (str): Subreddit name
        webhook (int, str): Webhook name, url, ID or None.

    Returns:
        Row['id', 'name', 'url'] | None
    """
    # Check if the subreddit exists
    res: Row | None = await _get_subreddit(name=name)
    if res is None:
        return None
    else:
        # Check if the webhook exists
        if type(webhook) == str and webhook.lower() == "none":
            webhook_id = None
        else:
            res = await _get_webhook(webhook)
            if res is None:
                return None
            else:
                webhook_id = res["ID"]

    async with asqlite.connect(DB_PATH) as db:
        async with db.cursor() as cur:
            await cur.execute("""UPDATE subreddit SET webhook_id = ? WHERE name = ?  RETURNING *""", webhook_id, name)
            await db.commit()
            res = await cur.fetchone()
            return res if not None else None


async def _get_webhook(arg: Union[str, int, None]) -> Row | None:
    """
    Lookup a Row in the Webhook Table

    Args:
        arg (Union[str, int, None]): Supports webhook name, id or url queries.

    Returns:
        Row["name", "id", "url"] | None
    """
    async with asqlite.connect(DB_PATH) as db:
        async with db.cursor() as cur:
            if arg is None:
                return None
            elif type(arg) == int:
                await cur.execute("""SELECT name, id, url FROM webhook where id = ?""", arg)
            elif type(arg) == str and arg.startswith("http"):
                await cur.execute("""SELECT name, id, url FROM webhook where url = ?""", arg)
            else:
                await cur.execute("""SELECT name, id, url FROM webhook where name = ?""", arg)
            res: Row | None = await cur.fetchone()
            await cur.close()
            return res if not None else None


async def _add_webhook(name: str, url: str) -> Row | None:
    """
    Add a Row to the Webhook Table.

    Args:
        name (str): A string to represent the Webhook URL in the table.
        url (str): Discord webhook URL

    Returns:
        Row['id','name','url'] | None 
    """
    res: Row | None = await _get_webhook(arg=url)
    if res is not None:
        return None
    else:
        async with asqlite.connect(DB_PATH) as db:
            async with db.cursor() as cur:
                await cur.execute("""INSERT INTO webhook(name, url) VALUES(?, ?) ON CONFLICT(url) DO NOTHING RETURNING *""", name, url)
                await db.commit()
                res = await cur.fetchone()
                await cur.close()
                return res if not None else None


async def _del_webook(arg: Union[int, str]) -> int | None:
    """
    Delete a Row matching the args from the Webhook Table.\n

    Converts string numbers into `ints` if they are digits.

    Args:
        arg (Union[int, str]): Supports Webhook ID, Name or URL.

    Returns:
        int | None: Row Count
    """
    if isinstance(arg, str) and arg.isdigit():
        arg = int(arg)
    res = await _get_webhook(arg=arg)
    if res is None:
        return None
    else:
        async with asqlite.connect(DB_PATH) as db:
            async with db.cursor() as cur:
                await cur.execute("""UPDATE subreddit SET webhook_id = ? WHERE webhook_id = ?  RETURNING *""", None, res["id"])
                await db.commit()
                await cur.execute("""DELETE FROM webhook WHERE id = ?""", res["id"])
                await db.commit()
                await db.close()
                return cur.get_cursor().rowcount


async def _get_all_webhooks() -> list[None | Webhook]:
    """
    Gets all Webhook Table Rows.

    Returns:
        list[Any] | list[dict[str, str | int]]: Structure `[{"name": str , "url": str , "id": int}]`
    """
    _webhooks: list[None | Webhook] = []
    async with asqlite.connect(DB_PATH) as db:
        async with db.cursor() as cur:
            await cur.execute("""SELECT name, id, url FROM webhook""")
            res: list[Row] = await cur.fetchall()
            if res is None or len(res) == 0:
                return []
            for entry in res:
                webhook: Webhook = {"name": entry["name"], "url": entry["url"], "id": entry["id"]}
                _webhooks.append(webhook)
                # _webhooks.append({"name": entry["name"], "url": entry["url"], "id": entry["id"]})
        await db.close()
        return _webhooks


class Reddit_IS(cog.KumaCog):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot=bot)
        self._file_dir = Path(__file__).parent
        self._message_timeout = 120

        self._interrupt_loop: bool = False
        self._delay_loop: float = 1
        self._loop_iterations: int = 0

        self._sessions = aiohttp.ClientSession()

        # Used to possible keep track of edits to the DB to prevent un-needed DB lookups on submissions.
        self._recent_edit: bool = False

        # Image hash and Url storage
        self._json: Path = self._file_dir.joinpath("reddit.json")
        self._url_list: list = []  # []  # Recently sent URLs
        self._hash_list: list = []  # []  # Recently hashed images
        self._url_prefixs: tuple[str, ...] = ("http://", "https://")

        # Edge Detection comparison.
        self._pixel_cords_array: list[list[tuple[int, int]]] = []
        self.IMAGE_COMP = ImageComp.Image_Comparison()

        # This is how many posts in each subreddit the script will look back.
        # By default the subreddit script looks at subreddits in `NEW` listing order.
        self._submission_limit = 30
        self._subreddits: list[Union[Any, dict[str, Union[str, None]]]] = []  # {"subreddit" : "webhook_url"}

        self._last_check: datetime = datetime.now(tz=timezone.utc)

        # This forces the timezone to change based upon your OS for better readability in your prints()
        # This script uses `UTC` for functionality purposes.
        self._system_tz = tzlocal.get_localzone()

        # Need to use the string representation of the time zone for `pytz` eg. `America/Los_Angeles`
        self._pytz = pytz.timezone(str(self._system_tz))

        # Purely used to fill out the user_agent parameter of PRAW
        self._sysos: str = sys.platform.title()
        # Used by URL lib for Hashes.
        self._User_Agent = UserAgent().chrome

        # Default value; change in `reddit_cog.ini`
        self._user_name: Union[str, None] = "Reddit Scrapper"

    async def cog_load(self) -> None:
        """Creates Sqlite Database if not present. 

            Gets settings from`reddit_cog.ini`

            Creates`reddit.json`if not present and Gets URL and Hash lists.

            Creates our _subreddit list.
            """
        # Setup our DB tables
        async with asqlite.connect(DB_PATH) as db:
            await db.execute(SUBREDDIT_SETUP_SQL)
            await db.execute(WEBHOOK_SETUP_SQL)
        # Grab our PRAW settings

        await self._ini_load()
        # Grab our hash/url DB
        try:
            self._last_check = self.json_load()
        except:
            self._last_check: datetime = datetime.now(tz=timezone.utc)

        self._subreddits = await _get_all_subreddits()

        if self.check_loop.is_running() is False:
            self.check_loop.start()

    async def cog_unload(self) -> None:
        """Saves our URL and hash list,
        stops our Scrapper loop if running and
        closes any open connections."""
        self.json_save()
        await self._reddit.close()
        await self._sessions.close()
        if self.check_loop.is_running():
            self.check_loop.stop()

    async def _ini_load(self) -> None:
        """Gets the Reddit login information and additional settings."""
        _setting_file: Path = self._file_dir.joinpath("reddit_cog.ini")
        if _setting_file.is_file():
            settings = ConfigParser(converters={"list": lambda setting: [value.strip() for value in setting.split(",")]})
            settings.read(_setting_file.as_posix())
            # PRAW SETTINGS
            _reddit_secret: str = settings.get("PRAW", "reddit_secret")
            _reddit_client_id: str = settings.get("PRAW", "reddit_client_id")
            _reddit_username: str = settings.get("PRAW", "reddit_username")
            _reddit_password: str = settings.get("PRAW", "reddit_password")

            # CONFIG
            _temp_name = settings.get("CONFIG", "username")
            if len(_temp_name):
                self._user_name = _temp_name

            self._reddit = asyncpraw.Reddit(
                client_id=_reddit_client_id,
                client_secret=_reddit_secret,
                password=_reddit_password,
                user_agent=f"by /u/{_reddit_username})",
                username=_reddit_username
            )
        else:
            raise ValueError("Failed to load .ini")

    def json_load(self) -> datetime:
        """
        Loads our last_check, url_list and hash_list from a json file; otherwise creates the file and sets our intial last_check time.

        Returns:
            datetime: The last check unix timestamp value from the json file.
        """
        last_check: datetime = datetime.now(tz=timezone.utc)

        if self._json.is_file() is False:
            with open(self._json, "x") as jfile:
                jfile.close()
                self.json_save()
                return last_check

        else:
            with open(self._json, "r") as jfile:
                data = json.load(jfile)
                # self._logger.info('Loaded our settings...')

        if 'last_check' in data:
            if data['last_check'] == 'None':
                last_check = datetime.now(tz=timezone.utc)
            else:
                last_check = datetime.fromtimestamp(
                    data['last_check'], tz=timezone.utc)
            # self._logger.info('Last Check... Done.')

        if 'url_list' in data:
            self._url_list = list(data['url_list'])
            # self._logger.info('URL List... Done.')

        if 'hash_list' in data:
            self._hash_list = list(data['hash_list'])
            # self._logger.info('Hash List... Done.')

        jfile.close()
        return last_check

    def json_save(self) -> None:
        """
        I generate an upper limit of the list based upon the subreddits times the submissin search limit; 
        this allows for configuration changes and not having to scale the limit of the list
        """
        _temp_url_list: list[str] = []
        _temp_hash_list: list[str] = []

        limiter: int = (len(self._subreddits) * self._submission_limit) * 3

        # Turn our set into a list, truncate it via indexing then replace our current set.
        if len(self._url_list) > limiter:
            # 'Trimming down url list...'
            _temp_url_list = self._url_list
            _temp_url_list = _temp_url_list[len(self._url_list) - limiter:]
            self._url_list = _temp_url_list

        if len(self._hash_list) > limiter:
            # 'Trimming down hash list...
            _temp_hash_list = self._hash_list
            _temp_hash_list = _temp_hash_list[len(self._hash_list) - limiter:]
            self._hash_list = _temp_hash_list

        data = {
            "last_check": self._last_check.timestamp(),
            "url_list": self._url_list,
            "hash_list": self._hash_list
        }
        with open(self._json, "w") as jfile:
            json.dump(data, jfile)
            # 'Saving our settings...'
            jfile.close()

    async def autocomplete_subreddit(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        res: list[str | Any | dict[str, str | None]] = await _get_all_subreddits(webhooks=False)
        return [app_commands.Choice(name=subreddit, value=subreddit) for subreddit in res if type(subreddit) == str and current.lower() in subreddit.lower()][:25]

    async def autocomplete_webhook(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        res: list[None | Webhook] = await _get_all_webhooks()
        return [app_commands.Choice(name=webhook["name"], value=webhook["name"]) for webhook in res if webhook is not None and current.lower() in webhook["name"].lower()][:25]

    @tasks.loop(minutes=5)
    async def check_loop(self) -> None:
        if self._recent_edit:
            self._subreddits = []
            self._subreddits = await _get_all_subreddits()
            self._recent_edit = False

        if self._subreddits == None:
            self._logger.warn("No Subreddits found...")
            return

        count = await self.subreddit_media_handler(last_check=self._last_check)
        self._last_check = datetime.now(tz=timezone.utc)
        self.json_save()
        if count >= 1:
            self._logger.info(f'Finished Sending {str(count) + " Images" if count > 1 else str(count) + " Image"}')

    async def subreddit_media_handler(self, last_check: datetime) -> int:
        """Iterates through the subReddits Submissions and sends media_metadata"""
        count = 0
        img_url: Union[str, None] = None
        img_url_to_send: list[str] = []

        # We check self._subreddits in `check_loop`
        for entry in self._subreddits:
            for sub, url in entry.items():
                if url == None:
                    self._logger.warn(f"No Webhook URL for {sub}, skipping...")
                    continue

                if self._interrupt_loop:
                    return count

                res: int = await self.check_subreddit(subreddit=sub)
                if res != 200:
                    self._logger.warn(f"Failed to find the subreddit /r/{sub}, skipping entry. Value: {res}")
                    continue

                cur_subreddit: Subreddit = await self._reddit.subreddit(display_name=sub)
                # limit - controls how far back to go (true limit is 100 entries)
                async for submission in cur_subreddit.new(limit=self._submission_limit):
                    post_time: datetime = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                    # found_post = False
                    # self._logger.info(f'Checking subreddit {sub} -> submission title: {submission.title} submission post_time: {post_time.astimezone(self._pytz).ctime()} last_check: {last_check.astimezone(self._pytz).ctime()}')

                    if post_time >= last_check:  # The more recent time will be greater than..
                        # reset our img list
                        img_url_to_send = []
                        # Usually submissions with multiple images will be using this `attr`
                        # TODO - Support reddit gallery (temp url) https://www.reddit.com/gallery/18dx6q0
                        if hasattr(submission, "media_metadata"):
                            for key, img in submission.media_metadata.items():
                                # example {'status': 'valid', 'e': 'Image', 'm': 'image/jpg', 'p': [lists of random resolution images], 's': LN 105}
                                # This allows us to only get Images.
                                if "e" in img and img["e"] == 'Image':
                                    # example 's': {'y': 2340, 'x': 1080, 'u': 'https://preview.redd.it/0u8xnxknijha1.jpg?width=1080&format=pjpg&auto=webp&v=enabled&s=04e505ade5889f6a5f559dacfad1190446607dc4'}, 'id': '0u8xnxknijha1'}
                                    # img_url = img["s"]["u"]
                                    img_url_to_send.append(img["s"]["u"])
                                    continue

                                else:
                                    continue

                        elif hasattr(submission, "url_overridden_by_dest"):
                            if submission.url_overridden_by_dest.startswith(self._url_prefixs):
                                img_url_to_send.append(submission.url_overridden_by_dest)
                            else:
                                continue

                        else:
                            continue

                        if len(img_url_to_send) > 0:
                            for img_url in img_url_to_send:
                                if self._interrupt_loop:
                                    return count
                                if img_url in self._url_list:
                                    continue

                                self._url_list.append(img_url)

                                # status: bool = await self.hash_process(img_url)
                                self._logger.info("checking edges")
                                status: bool = await self.partial_edge_comparison(img_url=img_url)

                                if status:
                                    # found_post = True
                                    count += 1
                                    await self.webhook_send(url=url, content=f'**r/{sub}** ->  __[{submission.title}]({submission.url})__\n{img_url}\n')

                                    time.sleep(self._delay_loop)  # Soft buffer delay between sends to prevent rate limiting.

                # if found_post == False:
                    # self._logger.info(f'No new Submissions in {sub} since {last_check.ctime()}(UTC)')

        return count

    async def partial_edge_comparison(self, img_url: str) -> bool:
        """
        Uses aiohttp to `GET` the image url, we modify/convert the image via PIL and get the edges as a list of tuples.\n
        We take a partial of the edges list and see if the entries exists in `_pixel_cords_array`. \n

        If a partial match is found; we run `full_edge_comparison`. If no full match found; we add the edges to `_pixel_cords_array`.

        Args:
            img_url (str): Web url to Image.

        Returns:
            bool: Returns `False` on any failed methods/checks or the array already exists else returns `True`.
        """
        stime: float = time.time()
        res: ClientResponse | Literal[False] = await self._get_img(img_url=img_url)
        if res == False:
            self._logger.error(f"Failed to `_get_img` {img_url}")
            return False

        source: Image = IMG.open(io.BytesIO(await res.read()))
        source = self.IMAGE_COMP._convert(image=source)
        res_image: tuple[Image, Image | None] = self.IMAGE_COMP._image_resize(source=source)
        source = self.IMAGE_COMP._filter(image=res_image[0])
        edges: list[tuple[int, int]] | None = self.IMAGE_COMP._edge_detect(image=source)
        if edges == None:
            return False
        # We set our max match req to do a full comparison via len of edges. eg. 500 * 10% (50 points)
        # We also set our min match req before we "abort"             eg. (50 * 90%) / 100 (40 points)
        # Track our failures and break if we exceed the diff.                    eg. 50-40 (10 points)
        match_req: int = int((len(edges) / self.IMAGE_COMP.sample_percent))
        min_match_req: int = int((match_req * self.IMAGE_COMP.match_percent) / 100)
        for array in self._pixel_cords_array:
            match_count: int = 0
            failures: int = 0
            # [(1,1),(2,2)(2,3)]
            for cords in range(0, len(edges), self.IMAGE_COMP.sample_percent):
                if match_count >= min_match_req:
                    match: bool = await self.full_edge_comparison(array=array, edges=edges)
                    if match == False:
                        break
                    else:
                        return False
                elif failures > (match_req - min_match_req):
                    break
                elif edges[cords] not in array:
                    failures += 1
                else:
                    match_count += 1

        # TODO - Store this array to a file for future reference.
        self._pixel_cords_array.append(edges)
        self._etime: float = (time.time() - stime)
        print("comparsion time", self._etime)
        return True

    async def full_edge_comparison(self, array: list[tuple[int, int]], edges: list[tuple[int, int]]) -> bool:
        """
        Similar to `partial_edge_comparison` but we use the full list of the image edges against our array

        Args:
            array (list[tuple[int, int]]): List of tuple's containing pixel cords. (X,Y)
            edges (list[tuple[int, int]]): List of tuple's containing pixel cords. (X,Y)

        Returns:
            bool: Returns `False` if the pixel cords are not in the array else `True` if the pixel cords are in the array.
        """
        failures: int = 0
        match_count: int = 0
        match_req: int = int((len(edges) / self.IMAGE_COMP.match_percent) / 100)
        min_match_req: int = len(edges) - match_req
        for cords in edges:
            if match_count >= min_match_req:
                return True
            elif failures > min_match_req:
                return False
            elif cords not in array:
                failures += 1
            else:
                match_count += 1
        return False

    async def _get_img(self, img_url: str) -> ClientResponse | Literal[False]:
        """
        Calls a `.get()` method to get the image data.

        Args:
            img_url (str): Web URL for the image.

        Returns:
            ClientResponse | Literal[False]: Returns data if it exists otherwise bool.
        """
        # req = urllib.request.Request(url=img_url, headers={'User-Agent': str(self._User_Agent)})

        try:
            req: ClientResponse = await self._sessions.get(url=img_url)

            # req_open = urllib.request.urlopen(req)
        except Exception as e:
            self._logger.error(f'Unable to handle {img_url} with error: {e}')
            return False

        # if 'image' in req_open.headers.get_content_type():
        if "Content-Type" not in req.headers:
            self._logger.error(f"Unable to find the Content-Type for {img_url}")
            return False

        if "image" in req.headers["Content-Type"]:
            return req

        else:  # Failed to find a 'image'
            self._logger.warn(f'URL: {img_url} is not an image -> {req.headers}')
            return False

    async def hash_process(self, img_url: str) -> bool:
        """
        Checks the Hash of the supplied url against our hash list.

        Args:
            img_url (str): Web URL for the image.
        Returns:
            bool: `True` if the sha256 is not in our list; otherwise `False`.
        """
        res = await self._get_img(img_url=img_url)
        my_hash = None
        if isinstance(res, bytes):
            my_hash = hashlib.sha256(await res.read()).hexdigest()
        if my_hash not in self._hash_list or my_hash != False:
            self._hash_list.append(my_hash)
            return True

        else:
            # self._logger.info('Found a duplicate hash...')
            return False

    async def webhook_send(self, url: str, content: str) -> bool | None:
        """
        Sends content to the Discord Webhook url provided.

        Args:
            url(str): The Webhook URL to use.
            content(str): The content to send to the url.
        Returns:
            bool | None: Returns `False` due to status code not in range `200 <= result.status < 300` else returns `None`
        """
        # Could check channels for NSFW tags and prevent NSFW subreddits from making it to those channels/etc.

        data = {"content": content, "username": self._user_name}
        result: ClientResponse = await self._sessions.post(url, json=data)
        if 200 <= result.status < 300:
            return

        else:
            # Attempting to dynamically adjust the delay in sending messages.
            if result.status == 429:
                self._delay_loop += .1

            self._loop_iterations += 1
            if self._loop_iterations > 10:
                self._delay_loop -= .1
                self._loop_iterations = 0

            self._logger.warn(f"Webhook not sent with {result.status} - delay {self._delay_loop}s, response:\n{result.json()}")
            return False

    async def check_subreddit(self, subreddit: str) -> int:
        """
        Attempts a `GET` request of the passed in subreddit.
        Args:
            subreddit(str): The subreddit to check. Do not include the `/r/`. eg `NoStupidQuestions` 
        Returns:
            int: Returns status code
        """
        temp_url: str = f"https://www.reddit.com/r/{subreddit}/"
        check: ClientResponse = await self._sessions.head(url=temp_url)
        return check.status

    @commands.hybrid_command(help="Add a subreddit to the DB", aliases=["rsadd", "rsa"])
    @app_commands.describe(sub="The sub Reddit name.")
    async def add_subreddit(self, context: commands.Context, sub: str):

        status: int = await self.check_subreddit(subreddit=sub)
        if status != 200:
            return await context.send(content=f"Unable to find the subreddit `/r/{sub}`.\n *Status code: {status}*", delete_after=self._message_timeout)

        res: Row | None = await _add_subreddit(name=sub)

        if res is not None:
            self._recent_edit = True
            return await context.send(content=f"Added `/r/{sub}` to our database.", delete_after=self._message_timeout)
        else:
            return await context.send(content=f"Unable to add `/r/{sub}` to the database.", delete_after=self._message_timeout)

    @commands.hybrid_command(help="Remove a subreddit from the DB", aliases=["rsdel", "rsd"])
    @app_commands.describe(sub="The sub Reddit name.")
    @app_commands.autocomplete(sub=autocomplete_subreddit)
    async def del_subreddit(self, context: commands.Context, sub: str):
        res: int | None = await _del_subreddit(name=sub)
        self._recent_edit = True
        await context.send(content=f"Removed {res} {'subreddit' if res else ''}", delete_after=self._message_timeout)

    @commands.hybrid_command(help="Update a subreddit with a Webhook", aliases=["rsupdate", "rsu"])
    @app_commands.describe(sub="The sub Reddit name.")
    @app_commands.autocomplete(sub=autocomplete_subreddit)
    @app_commands.autocomplete(webhook=autocomplete_webhook)
    async def update_subreddit(self, context: commands.Context, sub: str, webhook: str):
        res: Row | None = await _update_subreddit(name=sub, webhook=webhook)
        if res is None:
            return await context.send(content=f"Unable to update `/r/{sub}`.", delete_after=self._message_timeout)
        else:
            self._recent_edit = True
            return await context.send(content=f"Updated `/r/{sub}` in our database.", delete_after=self._message_timeout)

    @commands.hybrid_command(help="List of subreddits", aliases=["rslist", "rsl"])
    async def list_subreddit(self, context: commands.Context):
        res: list[Any | dict[str, str]] = await _get_all_subreddits()
        temp_list = []
        for entry in res:
            for name, webhook in entry.items():
                emoji = "\U00002705"  # White Heavy Check Mark
                if webhook is None:
                    emoji = "\U0000274c"  # Negative Squared Cross Mark
                sub_entry = f"{emoji} - **/r/**`{name}`"
                temp_list.append(sub_entry)

        return await context.send(content="**__Current Subreddit List__:**\n" + "\n".join(temp_list))

    @commands.hybrid_command(help="Info about a subreddit", aliases=["rsinfo", "rsi"])
    @app_commands.describe(sub="The sub Reddit name.")
    @app_commands.autocomplete(sub=autocomplete_subreddit)
    async def info_subreddit(self, context: commands.Context, sub: str):
        res: Row | None = await _get_subreddit(name=sub)
        if res is not None:
            wh_res: Row | None = await _get_webhook(arg=res["webhook_id"])
            if wh_res is not None:
                return await context.send(content=f"**Info on Subreddit /r/`{sub}`**\n> __Webhook Name__: {wh_res['name']}\n> __Webhook ID__: {wh_res['id']}\n> {wh_res['url']}", delete_after=self._message_timeout)
            else:
                return await context.send(content=f"**Info onSubreddit /r/`{sub}`**\n`No webhook assosciated with this subreddit`", delete_after=self._message_timeout)
        else:
            return await context.send(content=f"Unable to find /r/`{sub} in the database.", delete_after=self._message_timeout)

    @commands.hybrid_command(help="Add a webhook to the database.", aliases=["rswhadd", "rswha"])
    async def add_webhook(self, context: commands.Context, webhook_name: str, webhook_url: str):
        data: str = f"Testing webhook {webhook_name}"
        success: bool | None = await self.webhook_send(url=webhook_url, content=data)
        if success == False:
            return await context.send(content=f"Failed to send a message to `{webhook_name}` via url. \n{webhook_url}")

        res: Row | None = await _add_webhook(name=webhook_name, url=webhook_url)
        if res is not None:
            self._recent_edit = True
            return await context.send(content=f"Added **{webhook_name}** to the database. \n> `{webhook_url}`", delete_after=self._message_timeout)
        else:
            return await context.send(content=f"Unable to add {webhook_url} to the database.", delete_after=self._message_timeout)

    @commands.hybrid_command(help="Remove a webhook from the database.", aliases=["rswhdel", "rswhd"])
    @app_commands.autocomplete(webhook=autocomplete_webhook)
    async def del_webhook(self, context: commands.Context, webhook: str):
        res: int | None = await _del_webook(arg=webhook)
        self._recent_edit = True
        await context.send(content=f"Removed {res} {'webhook' if res else ''}", delete_after=self._message_timeout)

    @commands.hybrid_command(help="List all webhook in the database.", aliases=["rswhlist", "rswhl"])
    async def list_webhook(self, context: commands.Context):
        res: list[Webhook | None] = await _get_all_webhooks()
        return await context.send(content="**Webhooks**:\n" + "\n".join([f"**{entry['name']}** ({entry['id']})\n> `{entry['url']}`\n" for entry in res if entry is not None]), delete_after=self._message_timeout)

    @commands.hybrid_command(help="Start/Stop the Scrapper loop", aliases=["rsloop"])
    async def scrape_loop(self, context: commands.Context, util: Literal["start", "stop", "restart"]):
        start: list[str] = ["start", "on", "run"]
        end: list[str] = ["stop", "off", "end"]
        restart: list[str] = ["restart", "reboot", "cycle"]
        status: str = ""

        if util in start:
            if self.check_loop.is_running():
                status = "already running."
            else:
                self.check_loop.start()
                status = "starting."

        elif util in end:
            if self.check_loop.is_running():
                self._interrupt_loop = True
                self.check_loop.stop()
                status = "stopping."
            else:
                status = "not currently running."

        elif util in restart:
            # Force cancel and start the loop. Clean up any open sessions.
            self.check_loop.cancel()
            await self._sessions.close()
            self.check_loop.start()
            status = "restarting."
        else:
            return await context.send(content=f"You must use these words {','.join(start)} | {','.join(end)}", delete_after=self._message_timeout)
        return await context.send(content=f"The Scrapper loop is {status}", delete_after=self._message_timeout)

    @commands.hybrid_command(help="Sha256 comparison of two URLs", aliases=["sha256", "hash"])
    async def hash_comparison(self, context: commands.Context, url_one: str, url_two: str | None):
        failed: bool = False
        res = await self._get_img(img_url=url_one)
        if res == False:
            failed = True

        elif url_two is not None:
            res_two = await self._get_img(img_url=url_two)
            if res_two == False:
                failed = True
            elif res == res_two:
                return await context.send(content=f"The Images match!", delete_after=self._message_timeout)
            elif res != res_two:
                return await context.send(content=f"The Images do not match.\n `{res}` \n `{res_two}`", delete_after=self._message_timeout)
        else:
            return await context.send(content=f"Hash: `{res}`", delete_after=self._message_timeout)

        if failed:
            return await context.send(content=f"Unable to hash the URLs provided.", delete_after=self._message_timeout)

    @commands.hybrid_command(help="Edge comparison of two URLs", aliases=["edge"])
    async def edge_comparison(self, context: commands.Context, url_one: str, url_two: str):
        res_one: ClientResponse | Literal[False] = await self._get_img(img_url=url_one)
        res_two: ClientResponse | Literal[False] = await self._get_img(img_url=url_two)
        if res_one and res_two != False:
            img_one: Image = IMG.open(io.BytesIO(await res_one.read()))
            img_two: Image = IMG.open(io.BytesIO(await res_two.read()))
            self.IMAGE_COMP.compare(source=img_one, comparison=img_two)
            return await context.send(f"{self.IMAGE_COMP.results}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Reddit_IS(bot))
