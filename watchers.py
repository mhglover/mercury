"""reaper task"""
import os
import logging
import asyncio
import datetime
from pprint import pformat
from models import User, WatcherState
from users import getuser, getplayer
from queue_manager import getnext, set_rec_expiration
from raters import rate, record, rate_by_position, get_track_ratings
from spot_funcs import trackinfo, queue_safely
from spot_funcs import is_saved


# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace, trailing-newlines
# pylint: disable=consider-using-f-string

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


async def watchman(taskset, cred, spotify, watcher, user):
    """start a watcher for active users"""
    procname=f"watchman_{user.displayname}"
    watchname=f"watcher_{user.displayname}"
    
    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager')
    if "spotify_watcher" not in run_tasks:
        logging.debug("%s this instance doesn't run spot watchers", procname)
        return
    
    if watchname in [x.get_name() for x in asyncio.all_tasks()]:
        logging.debug("%s is already running, won't start another", watchname)
        return

    logging.debug("%s creating a spotify watcher for: %s", 
                    procname, watchname)
    user_task = asyncio.create_task(watcher(cred, spotify, user),
                    name=watchname)
    
    # add this user task to the global tasks set
    added = taskset.add(user_task)
    if added is not None:
        logging.error("%s failed adding task to taskset?")


async def spotify_watcher(cred, spotify, user):
    """start a long-running task to monitor a user's spotify usage, rate and record plays"""    
    
    state = WatcherState(cred=cred, user=user, spotify=spotify)
    state.user, state.token = await getuser(cred, user)
    
    procname = f"watcher_{state.user.displayname}"
    logging.info("%s watcher starting", procname)
    
    # set the watcherid for the spotwatcher process
    await state.set_watcher_name()

    # Loop while alive
    while state.ttl > datetime.datetime.now(datetime.timezone.utc):

        # see what the user's player is doing
        state.currently = await getplayer(state)
        
        # if anything else weird is happening, sleep for a minute and then loop
        if state.status != "active":
            state.status = state.user.status
            logging.info("%s player is not active, sleeping: %s", procname, state.user.status)
            await asyncio.sleep(60)
            continue

        # refresh the ttl, token, do some math, etc
        await state.refresh()
        
        # pull details for the next track in the queue
        state.nextup = await getnext()

        # what track are we currently playing?
        state.track = await trackinfo(spotify, state.currently.item.id)
        state.rating = await get_track_ratings(state.track, [state.user])
        state.is_this_saved = await is_saved(state.spotify, state.token, state.track)
        value = 4 if state.is_this_saved else 1

        # if the track hasn't changed but the savestate has, rate it love/like
        if not state.track_changed() and state.savestate_changed():
            await rate(state.user, state.track, value=value, downrate=True)
            logging.info("%s savestate rated (%s) %s", procname, value, state.t())
        
        # has anybody set this rec to expire yet? no? I will.
        if not state.next_has_expiration():
            
            # set it for approximately our endzone, which we can calculate pretty closely
            await set_rec_expiration(state.nextup, state.remaining_ms)
            logging.info("%s -- recommendation started, expiration set - %s", procname, state.t())
            
            # record a PlayHistory when we set the expiration on a recommendation
            await record(state.user, state.nextup.track)
            logging.info("%s recorded play history %s", procname, state.t())
            
        if state.track_changed():
            logging.info("%s -- track change -- %s%% %s ", 
                            procname, state.last_position, state.last_track.trackname)
            logging.info("%s -- now playing -- %s", procname, state.track.trackname) 
            
            # if we didn't finish cleanly, rate tracks based on last known position
            if not state.finished:
                await rate_by_position(user, state.last_track, state.last_position, value=value)
            
            # unset so we can handle the next track properly
            state.finished = False

        if state.endzone: # welcome to the end zone
            
            # if we're listening to the endzone of the upcoming rec, it's time to remove it
            if state.next_is_now_playing():
                await state.nextup.delete()
                logging.info("%s removed track from Recommendations: %s", procname, state.n())
                
                # but now get the real next queued track
                state.nextup = await getnext()

            # let's wrap this up - this should only run once while in the endzone, not every loop
            if not state.finished:
                # set a rating
                value = 4 if state.is_this_saved else 1
                await rate(state.user, state.track, value=value)
                logging.info("%s -- finishing track %s (%s)", procname, state.t(), value)
                
                # queue up the next track unless there are good reasons
                await queue_safely(state.spotify, state.token, state)
                
                # unset this when we detect a track change
                state.finished = True
        
        # set values for next loop
        state.last_track = state.track
        state.last_position = state.position
        state.was_saved = state.is_this_saved
        
        logging.info("%s sleeping %0.2ds - %s %s %d%%",
                        procname, state.sleep, state.t(),
                        state.displaytime, state.position)
        await asyncio.sleep(state.sleep)

    # ttl expired, clean up before exit
    await state.cleanup()
    logging.info("%s exiting", procname)
    return procname
