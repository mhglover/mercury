"""reaper task"""
import os
import logging
import asyncio
import datetime
from models import User, WatcherState
from users import getuser, getplayer
from queue_manager import getnext, set_rec_expiration
from raters import rate, record, rate_by_position, get_track_ratings
from spot_funcs import trackinfo, queue_safely
from spot_funcs import is_saved


# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace, trailing-newlines


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
    
    logging.debug("fetching user for watcher: %s", user.displayname)
    state = WatcherState(cred=cred, user=user)
    state.user, token = await getuser(cred, user)
    
    procname = f"watcher_{state.user.displayname}"
    logging.info("%s watcher starting", procname)

    # Loop while alive
    logging.debug("%s starting loop", procname)
    while state.ttl > datetime.datetime.now(datetime.timezone.utc):
        logging.debug("%s loop is awake", procname)
        
        # get the user's updated details and if necessary refresh the token
        state.user, state.token = await getuser(cred, state.user)
        
        # set the watcherid for the spotwatcher process
        state.user.watcherid = (f"watcher_{state.user.spotifyid}_" 
                                + f"{datetime.datetime.now(datetime.timezone.utc)}")
        await state.user.save()

        # see what the user's player is doing
        with spotify.token_as(token):
            state.currently = await getplayer(spotify, token, state.user)
        
        if state.currently == 401:
            # player says we're no longer authorized, let's error and exit
            logging.error("401 unauthorized from spotify player, breaking")
            break

        # if anything else weird is happening, sleep until the next loop
        if state.user.status != "active":
            logging.debug("%s player is not active, sleeping: %s", procname, state.user.status)
            continue
            
        # keep the watcher alive for 20 minutes as long as we're playing
        state.ttl = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=20)
        logging.debug("%s updating ttl, last_active and status: %s", procname, state.ttl)
        state.user.last_active = datetime.datetime.now(datetime.timezone.utc)
        await state.user.save()
            
        # update the ttl on followers too
        for each in await User.filter(status=f"following:{state.user.displayname}"):
            each.last_active = datetime.datetime.now(datetime.timezone.utc)
            await each.save()
        
        # pull details for the next track in the queue
        state.nextup = await getnext()

        # what track are we currently playing?
        state.track = await trackinfo(spotify, state.currently.item.id)
        state.is_this_saved = await is_saved(spotify, token, state.track)
        state.rating = await get_track_ratings(state.track, [state.user])
        
        # do some math
        state.position = int((state.currently.progress_ms/state.currently.item.duration_ms) * 100)
        state.remaining_ms = state.currently.item.duration_ms - state.currently.progress_ms
        state.displaytime = "{:}:{:02}".format(*divmod(state.remaining_ms // 1000, 60)) # pylint: disable=consider-using-f-string

        # if the track hasn't changed but the savestate has, rate it love/like
        if ( state.was_saved is not None and 
            not state.track_changed() and 
            state.was_saved != state.is_this_saved):
            
            # set a rating
            value = 4 if await is_saved(spotify, token, state.track) else 1
            await rate(state.user, state.track, value=value, downrate=True)
            logging.info("%s savestate changed, autorating (%s) [%s][%s] %s", 
                            procname, value, state.track.id, state.track.spotifyid, state.t())
        
        # has anybody set this rec to expire yet? no? I will.
        if (state.nextup                                  # we've got a Recommendation
            and state.nextup.track.id == state.track.id         # that we're currently playing
            and state.nextup.expires_at is None           # and nobody has set the expiration yet
            ):
            
            # set it for approximately our endzone, which we can calculate pretty closely
            await set_rec_expiration(state.nextup, state.remaining_ms)
            logging.info("%s set expiration for %s", procname, state.t())
            
            # record a PlayHistory only when we set the expiration on a recommendation? 
            await record(state.user, state.nextup.track)
            logging.info("%s recorded play history %s", procname, state.t())

        # we aren't in the endzone yet
        state.update_endzone_status()
        if not state.endzone:
            state.calculate_sleep_duration()

            # detect track changes
            # move this into a state function
            if state.track_changed():
                logging.info("%s track change at %s%% - now playing %s",
                            procname, state.last_position, state.track.trackname)
                
                # after a track change, rate tracks based on last known position
                value = 4 if state.is_this_saved else 1
                await rate_by_position(user, state.last_track, state.last_position, value=value)

        # welcome to the end zone
        else:
            
            # if we're listening to the upcoming rec, it's time to remove it
            if state.next_is_now_playing():
                await state.nextup.delete()
                logging.info("%s removed track from Recommendations: %s", procname, state.n())
                
                # now get the real next queued track
                state.nextup = await getnext()
                logging.info("%s new nextup track from Recommendations: %s", 
                             procname, state.nextup.trackname)
            
            logging.info("%s endzone %s - next up %s", procname, state.t(), state.n())

            # set a rating
            value = 4 if await is_saved(spotify, token, state.track) else 1
            await rate(state.user, state.track, value=value)
            logging.info("%s rating (%s) [%s][%s] %s", 
                            procname, value, state.track.id, state.track.spotifyid,
                            state.t())

            # queue up the next track unless there are good reasons
            await queue_safely(spotify, token, state)

            # zeno's cigarette breaks keep getting shorter
            # sleep for half the remaining time
            state.sleep = state.remaining_ms / 2 / 1000
        
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
