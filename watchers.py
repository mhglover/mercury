"""reaper task"""
import logging
import asyncio
import datetime
# from tortoise.expressions import Q
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
        # actives = await User.filter(last_active__gte=interval).exclude(status="inactive")
        expired  = await User.filter(last_active__lte=interval).exclude(status="inactive")
        # for user in actives:
        #     interval = datetime.datetime.now(datetime.timezone.utc) - user.last_active
        #     logging.info("%s active user %s last active %s ago",
        #                 procname, user.spotifyid, interval)
        for user in expired:
            logging.info("%s marking user as inactive after %s minutes idle: %s",
                        procname, idle, user.spotifyid)
            user.status = "inactive"
            await user.save(update_fields=['status'])
        await asyncio.sleep(sleep)



async def watchman(taskset, watcher, userid=None):
    """check the database regularly and start a watcher for active users"""
    procname="watchman"
    sleep = 10
    if userid is not None:
        user = await User.get(spotifyid=userid)
        logging.info("%s creating a spotify watcher task for: %s", 
                        procname, user.spotifyid)
        user_task = asyncio.create_task(watcher(user.spotifyid),
                        name=f"watcher_{user.spotifyid}")

        # add this user task to the global tasks set
        taskset.add(user_task)

        # To prevent keeping references to finished tasks forever,
        # make each task remove its own reference from the set after
        # completion:
        user_task.add_done_callback(taskset.remove(user_task))
    else:
        while True:
            procname="night_watchman"
            logging.info("%s checking for active users without watchers every %s seconds",
                          procname, sleep)
            actives = User.filter(status="active")
            for user in actives:
                interval = datetime.datetime.now(datetime.timezone.utc) - user.last_active
                logging.info("%s active user %s last active %s ago",
                            procname, user.spotifyid, interval)
            
            await asyncio.sleep(sleep)
    
    logging.info("%s exiting", procname)
    