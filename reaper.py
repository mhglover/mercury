"""reaper task"""
import logging
import asyncio
import datetime
from models import User

# pylint: disable=trailing-whitespace

async def user_reaper():
    """check the database every 5 minutes and remove inactive users"""
    procname="user_reaper"
    sleep = 600
    while True:
        logging.info("%s checking for inactive users every %s seconds", procname, sleep)
        interval = datetime.datetime.now() - datetime.timedelta(minutes=20)
        inactives  = await User.filter(last_active__lte=interval)
        for user in inactives:
            logging.info("%s marking user as inactive after 20 minutes idle %s",
                        procname, user.spotifyid)
            user.active_now=False
            await user.save
        await asyncio.sleep(sleep)
