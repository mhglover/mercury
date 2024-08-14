"""reaper task"""
import os
import logging
import asyncio
from datetime import timezone as tz, datetime as dt, timedelta
from humanize import naturaltime
import tekore as tk
from models import User, WatcherState, Lock, Recommendation
from users import getuser, getplayer
from queue_manager import getnext, set_rec_expiration
from raters import rate, record_history, rate_by_position, get_rating
from spot_funcs import trackinfo, queue_safely, is_saved, get_player_queue, send_to_player

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
    user_task.add_done_callback(taskset.remove)
    if added is not None:
        logging.error("%s failed adding task to taskset?")


async def spotify_watcher(cred, spotify, user):
    """start a long-running task to monitor a user's spotify usage, rate and record plays"""    
    
    logging.info("%s watcher starting", user.displayname)
    
    state = WatcherState(cred=cred, user=user, spotify=spotify)
    state.user, state.token = await getuser(cred, user)
    await Lock.release_all_user_locks(state.user.id)
    
    procname = f"watcher_{state.user.displayname}"
    
    await state.set_watcher_name()

    while (state.ttl > dt.now(tz.utc) and state.user.status == 'active'):

        # first thing is to get the current state of the player, user, recommendations, etc
        state.currently = await getplayer(state)
        
        # if if the player is not active, sleep for a bit and check again
        if state.status != "active":
            state.sleep = 10
            logging.debug("%s player: %s - user: %s - ttl: %s - sleep %ss",
                         procname, state.status, state.user.status,
                         naturaltime(state.ttl), state.sleep)
            await asyncio.sleep(state.sleep)
            # update the user record from the database
            state.user = await User.get(id=state.user.id)
            continue

        # if we're awake, update the loop ttl, sleep time, etc
        await state.refresh()
        state.track = await trackinfo(spotify, state.currently.item.id, token=state.token)
        state.rating = await get_rating(state.user, state.track.id)
        state.is_saved = await is_saved(state.spotify, state.token, state.track)
        value = 4 if state.is_saved else 1
        
        # detect track changes and rate the previous track
        if state.track_last_cycle.id and state.track_changed():
            
            logging.info("%s track change from %s at %s%% to %s (%s)",
                         procname, state.l(), state.position_last_cycle, 
                         state.track.trackname, state.reason)
            
            # if we didn't finish cleanly, rate tracks based on last known position
            if state.track_last_cycle.id is not None and not state.finished:
                await rate_by_position(user, state.track_last_cycle, 
                                       state.position_last_cycle)
                
                # position-rate for followers as well, will downrate if necessary
                followers = await User.filter(watcherid=user.id)
                for f in followers:
                    logging.info("%s position-rating %s for %s", procname, value, f.displayname)
                    await rate_by_position(f, state.track_last_cycle, 
                                           state.position_last_cycle)
            
            # unset some states so we can handle the next track properly
            state.history = None
            state.recorded = state.finished = state.just_rated = False

        # every watcher will write a history record for every track they see playing while active
        if not state.history or state.history.track_id != state.track.id:
            # record a PlayHistory when this watcher sees a track playing that doesn't match the state.history
            state.history = await record_history(state)
            state.recorded = True
        
        # if the track hasn't changed but the savestate has, rate it love/like
        if ((state.track_last_cycle.id == state.track.id) and 
            (state.was_saved_last_cycle != state.is_saved)):
        
            state.rating.rating = value
            await state.rating.save()
            state.just_rated = True
            logging.info("%s savestate rated (%s) %s", procname, value, state.t())
        
        # get the current list of recommended tracks
        recs = await Recommendation.all().prefetch_related("track")
        
        # if we're playing a rec, pop that object of the recs list
        rec = next((rec for rec in recs if rec.track_id == state.track.id), None)
        
        # if we're playing a recommendation that doesn't have an expiration
        # set the expiration and note the reason it was selected
        if rec and rec.expires_at is None:
            logging.info("%s playing rec: %s", procname, rec.trackname)
            # note the reason we're playing this track
            state.reason = rec.reason
            
            logging.debug("%s recommendation first started, setting expiration: %s", procname, state.t())
            
            expiration_interval = timedelta(milliseconds=(state.remaining_ms - 30000))
            rec.expires_at = dt.now(tz.utc) + expiration_interval
            await rec.save()

            logging.info("%s expiration set: %s - %s", procname, rec.trackname, rec.expires_at)

        # get a rough guess at what the next rec will be
        state.nextup = recs[0] if recs else None
        
        # see if we need to send a recommendation to the player
        _ = await queue_safely(state)
        
        if state.endzone: # welcome to the end zone
            
            # if we're listening to the endzone of the upcoming rec, it's time to remove it
            if state.next_is_now_playing():
                await state.nextup.delete()
                logging.debug("%s removed track from Recommendations: %s", procname, state.n())
                
                # but now get the real next queued track
                state.nextup = await getnext()

            # let's wrap this up - this should only run once while in the endzone, not every loop
            if not state.finished:
                # set a rating if it's still a shrug
                if state.rating.rating == 0:
                    logging.info("%s [track endzone] rating track %s (%s)", procname, state.track.trackname, value)
                    await rate(state.user, state.track, value=value)
                
                    # rate a 1 for followers when we finish a track - won't auto downrate
                    followers = await User.filter(watcherid=user.id)
                    fvalue = 1
                    for f in followers:
                        logging.info("%s [track endzone] follower %s rating track (%s)", procname, f.displayname, f.value)
                        await rate(f, state.track, value=fvalue)
                
                # don't repeat this part of the loop until we detect a track change
                state.finished = True
        
        # set values for next loop
        state.track_last_cycle = state.track
        state.position_last_cycle = state.position
        state.was_saved_last_cycle = state.is_saved
        
        # update the user record from the database
        state.user = await User.get(id=state.user.id)
        
        logging.debug("%s sleeping %0.2ds - %s %s %d%%",
                        procname, state.sleep, state.t(),
                        state.displaytime, state.position)
        await asyncio.sleep(state.sleep)

    # ttl expired, clean up before exit
    await Lock.release_lock(procname)
    await state.cleanup()
    logging.info("%s exiting, user status: %s", procname, state.user.status)
    return procname
