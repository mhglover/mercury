"""reaper task"""
import logging
import asyncio
import datetime
from tortoise.expressions import Q
from models import User

# pylint: disable=trailing-whitespace

async def user_reaper():
    """check the database every 5 minutes and remove inactive users"""
    procname="user_reaper"
    sleep = 300
    idle = 20
    while True:
        logging.debug("%s checking for inactive users every %s seconds", procname, sleep)
        interval = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=idle)
        actives = await User.filter(last_active__gte=interval).exclude(active_now=False)
        inactives  = await User.filter(Q(last_active__lte=interval) | Q(active_now=False))
        for user in actives:
            interval = datetime.datetime.now(datetime.timezone.utc) - user.last_active
            logging.info("%s active user %s last active %s ago",
                        procname, user.spotifyid, interval)
        for user in inactives:
            logging.info("%s marking user as inactive after %s minutes idle: %s",
                        procname, idle, user.spotifyid)
            user.active_now = False
            await user.save(update_fields=['active_now'])
        await asyncio.sleep(sleep)
