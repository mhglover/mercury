"""reaper task"""
import os
import logging
import asyncio
from datetime import timezone as tz, datetime as dt, timedelta
from humanize import naturaltime
from models import User, WatcherState, Lock
from users import getuser, getplayer
from queue_manager import getnext, set_rec_expiration
from raters import rate, record, rate_by_position, get_rating
from spot_funcs import trackinfo, queue_safely, is_saved
from socket_funcs import queue_webuser_updates
from helpers import feelabout

async def user_reaper():
    """check the database every 5 minutes and remove inactive users"""
    procname="user_reaper"
    sleep = 300
    idle = 20
    while True:
        logging.debug("%s checking for inactive users every %s seconds", procname, sleep)
        interval = dt.now(tz.utc) - timedelta(minutes=idle)
        # actives = await User.filter(last_active__gte=interval).exclude(status="inactive")
        expired  = await User.filter(last_active__lte=interval).exclude(status="inactive")
        
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
    
    tasknames = [x.get_name() for x in asyncio.all_tasks()]
    if watchname in tasknames:
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
    
    # if we can't get a lock, another server or process is already watching this user
    try:
        if not await Lock.attempt_acquire_lock(user.id):
            logging.warning("another server is already watching user %s, not starting", 
                            user.displayname)
            return
    except Exception as e:
        logging.error("lock error: %s", e)
        return
    logging.info("%s watcher starting", user.displayname)
    
    state = WatcherState(cred=cred, user=user, spotify=spotify)
    state.user, state.token = await getuser(cred, user)
    
    procname = f"watcher_{state.user.displayname}"
    
    await state.set_watcher_name()

    while (state.ttl > dt.now(tz.utc) and state.user.status == 'active'):

        state.currently = await getplayer(state)
        
        # if anything else weird is happening, sleep for a minute and then loop
        if state.status != "active":
            state.sleep = 10
            logging.debug("%s player: %s - user: %s - ttl: %s - sleep %ss",
                         procname, state.status, state.user.status,
                         naturaltime(state.ttl), state.sleep)
            continue

        # refresh the ttl, token, do some math, etc
        await state.refresh()
        
        # pull details for the next track in the queue
        state.nextup = await getnext()

        # what track are we currently playing?
        state.track = await trackinfo(spotify, state.currently.item.id)
        
        state.is_saved = await is_saved(state.spotify, state.token, state.track)
        
        # if the track has changed, get the rating
        # if state.track_changed():
            
        state.rating = await get_rating(state.user, state.track.id)
        feel = feelabout(state.rating.rating)
        
        try:
            updates = [
            {"element_id": "currently", "attribute_type": 
                "value", "value": state.track.trackname},
            {"element_id": "currently", "attribute_type": "class", 
                "value": f"track-name {feel}"},
            {"element_id": "currently", "attribute_type": "href", 
                "value": f"/track/{state.track.id}"},
            {"element_id": "currently_ratedown", "attribute_type": "onclick", 
                "value": f"quickrate('{state.track.id}', 'currently_ratedown', '-1')"},
            {"element_id": "currently_rateup", "attribute_type": "onclick", 
                "value": f"quickrate('{state.track.id}', 'currently_rateup', '1')"}
            ]
        except Exception as e:
            logging.error("% - error updating currently playing: %s", procname, e)
            
        try:
            #update the user's currently playing trackname
            logging.info("%s sending currently update: %s", procname, state.track.trackname)
            await queue_webuser_updates(state.user.id, updates)
        
        except Exception as e:
            logging.error("error sending currently update: %s", e)
        
        # figure out the value for an autorate if we need it
        value = 4 if state.is_saved else 1

        # if the track hasn't changed but the savestate has, rate it love/like
        if not state.track_changed() and state.savestate_changed():
            state.rating.rating = value
            await state.rating.save()
            state.just_rated = True
            logging.info("%s savestate rated (%s) %s", procname, value, state.t())
        
        if state.history.track_id != state.track.id:
            # record a PlayHistory when we see a track for the first time
            state.history = await record(state)
            logging.info("%s recorded play history %s", procname, state.t())
            state.recorded = True
        
        # we're playing a rec! has anybody set this rec to expire yet? no? I will.
        if state.track.id == state.nextup.track.id and state.nextup.expires_at is None:
            logging.info("%s recommendation started %s, no expiration", procname, state.t())
            
            # set it for approximately our endzone, which we can calculate pretty closely
            expiration = await set_rec_expiration(state.nextup, state.remaining_ms)
            logging.info("%s expiration set %s", procname, naturaltime(expiration))
            
        if state.track_changed():
            logging.info("%s -- track change -- %s%% %s ", 
                            procname, state.position_last_cycle, state.track_last_cycle.trackname)
            logging.info("%s -- now playing -- %s", procname, state.track.trackname) 
            
            # if we didn't finish cleanly, rate tracks based on last known position
            if state.track_last_cycle.id is not None and not state.finished:
                await rate_by_position(user, state.track_last_cycle, 
                                       state.position_last_cycle, value=value)
            
            # unset some states so we can handle the next track properly
            state.recorded = state.finished = state.just_rated = False

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
                await rate(state.user, state.track, value=value)
                logging.info("%s finishing track %s (%s)", procname, state.t(), value)
                
                # rate a 1 for followers
                followers = await User.filter(watcherid=user.id)
                for f in followers:
                    await rate(f, state.track, value=1)
                
                # queue up the next track unless there are good reasons
                await queue_safely(state.spotify, state.token, state)
                
                # don't repeat this part of the loop until we detect a track change
                state.finished = True
        
        # set values for next loop
        state.track_last_cycle = state.track
        state.position_last_cycle = state.position
        state.was_saved_last_cycle = state.is_saved
        
        logging.debug("%s sleeping %0.2ds - %s %s %d%%",
                        procname, state.sleep, state.t(),
                        state.displaytime, state.position)
        await asyncio.sleep(state.sleep)

    # ttl expired, clean up before exit
    await Lock.release_lock(procname)
    await state.cleanup()
    logging.info("%s exiting, user status: %s", procname, state.user.status)
    return procname
