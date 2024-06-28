"""reaper task"""
import os
import logging
import asyncio
import datetime
from models import User, Recommendation, WatcherState
from users import getuser, getplayer
from queue_manager import getnext, set_rec_expiration
from raters import rate, record
from spot_funcs import trackinfo, send_to_player
from spot_funcs import is_already_queued, is_saved, was_recently_played, copy_track_data

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


    # else:
    #     while True:
    #         procname="night_watchman"
    #         logging.info("%s checking for active users without watchers every %s seconds",
    #                       procname, sleep)
    #         actives = User.filter(status="active")
    #         for user in actives:
    #             interval = datetime.datetime.now(datetime.timezone.utc) - user.last_active
    #             logging.info("%s active user %s last active %s ago",
    #                         procname, user.spotifyid, interval)
            
    #         await asyncio.sleep(sleep)
    


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
                                + "{datetime.datetime.now(datetime.timezone.utc)}")
        await state.user.save()

        # see what the user's player is doing
        with spotify.token_as(token):
            logging.debug("%s checking currently playing", procname)
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
            
        # note details from the last loop for comparison
        if state.track is not None:
            state.last_track = copy_track_data(state.track)
            state.last_position = float(state.position)
            state.was_saved = bool(state.is_this_saved)
            
        # pull details for the next track in the queue
        state.nextup = await getnext()

        # what track are we currently playing?
        state.track = await trackinfo(spotify, state.currently.item.id)
        state.is_this_saved = await is_saved(spotify, token, state.track)
        
        # do some math
        state.position = state.currently.progress_ms/state.currently.item.duration_ms
        state.remaining_ms = state.currently.item.duration_ms - state.currently.progress_ms
        state.displaytime = "{:}:{:02}".format(*divmod(state.remaining_ms // 1000, 60)) # pylint: disable=consider-using-f-string

        # if the track hasn't changed but the savestate has, rate it love/like
        logging.debug("is saved? %s - was saved? %s", state.is_this_saved, state.was_saved)
        if not state.track_changed() and state.was_saved != state.is_this_saved:
            
            # we saved it, so rate it a 4
            if state.is_this_saved:
                await rate(state.user, state.track, 4)
                logging.info("%s user %s just saved this track, autorating at 4",
                             procname, state.user.displayname)
                
            # we unsaved it, so rate it a 1
            else:
                await rate(state.user, state.track, 1, downrate=True)
                logging.info("%s user %s just un-saved this track, autorating down to 1",
                             procname, state.user.displayname)
        
        
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
        if state.remaining_ms > 30000:
            state.endzone = False

            # detect track changes
            # move this into a state function
            if (state.last_track and state.last_track.spotifyid is not None 
                and state.last_track.spotifyid != state.track.spotifyid):
                logging.info("%s track change at %.0d%% - now playing %s",
                            procname, state.last_position, state.track.trackname)
                
                # did we skip?
                # move this into a db_func
                if state.last_track.spotifyid == state.nextup.track.spotifyid:
                    logging.info("%s removing skipped track from recs: %s",
                                procname, state.last_track.trackname)
                    try:
                        await Recommendation.get(id=state.nextup.track_id).delete()
                    except Exception as e:
                        logging.error("%s exception removing track from recommendations\n%s",
                                    procname, e)
                
                # rate skipped tracks based on last position
                if state.last_position < 0.33:
                    value = -2
                    logging.info("%s early skip rating, %s %s %s",
                                state.user.spotifyid, state.last_track.trackname, value, procname)
                    await rate(state.user, state.last_track, value)
                elif state.last_position < 0.7:
                    value = -1
                    logging.info("%s late skip rating, %s %s %s",
                                state.user.spotifyid, state.last_track.spotifyid, value, procname)
                    await rate(state.user, state.last_track, value)
        
            # less than 30 seconds to the endzone, just sleep until then
            # move sleep calculation into state function
            if (state.remaining_ms - 30000) < 30000: 
                state.sleep = (state.remaining_ms - 30000) / 1000             
            # otherwise sleep for thirty seconds
            else: 
                state.sleep = 30

        # welcome to the end zone
        # put this magic number into Settings? at least a const
        elif state.remaining_ms <= 30000:
            
            # if we're listening to the next rec, remove the track from dbqueue
            # make this into a db_func
            if state.nextup and state.track.id == state.nextup.track.id:
                logging.info("%s removing track from Recommendations: %s",
                            procname, state.n())
                try:
                    await Recommendation.filter(id=state.nextup.id).delete()
                except Exception as e:
                    logging.error("%s exception removing track from upcomingqueue\n%s",
                                    procname, e)
                
                # now get the real next queued track
                state.nextup = await getnext()
            
            logging.info("%s endzone %s - next up %s", procname, 
                        state.t(), state.n())
            
            # if this is the first time we hit the endzone, 
            # let's do stuff that shouldn't be repeated
            if not state.endzone:
                # this is dumb trickery to prevent re-rating 
                # the track multiple times if paused during the endzone
                state.endzone = True
                
                # autorate based on whether or not this is a saved track
                state.is_saved = await is_saved(spotify, token, state.track)
                
                # move rating value into data model
                if state.is_saved:
                    value = 4
                else:
                    value = 1
            
                logging.info("%s rating (%s) [%s][%s] %s", 
                                procname, value, state.track.id, state.track.spotifyid,
                                state.t())
                
                # rework rate so it just needs the state by default and
                # allows keyword args for explicit rating
                await rate(state.user, state.track, value=value)

            # queue up the next track unless there are good reasons
            # move to db_func should_send_recommendation bool
            if state.nextup is None:
                # don't send a none
                logging.warning("%s no Recommendations, nothing to queue", procname)
            
            # don't queue the track we're currently playing, dingus
            elif state.track.id == state.nextup.track.id:
                logging.warning("%s track is playing now, won't send again, removing - %s",
                                procname, state.nextup.track.trackname)
                # and remove it from the queue
                await state.nextup.track.delete()
            
            # don't send a track we already played 
            # this may cause a problem down the road
            elif await was_recently_played(spotify, token, state.nextup.track.spotifyid):
                logging.warning("%s track was played recently, won't send again, removing - %s",
                                procname, state.nextup.track.trackname)
                # and remove it from the queue
                await state.nextup.track.delete()
            
            # don't resend something that's already in the player queue/context
            # figure out the context and ignore those tracks
            elif await is_already_queued(spotify, token, state.nextup.track.spotifyid):
                logging.warning("%s track already queued, won't send again - %s",
                                procname, state.nextup.track.trackname)
            
            # okay fine, queue it
            else:
                await send_to_player(spotify, token, state.nextup.track)
                logging.info("%s sent to player queue [%s] %s",
                                procname, state.nextup.reason, state.n())

                # sleep until this track is done
                state.sleep = (state.remaining_ms /1000) + 2
        
        
        state.user.status = f"{state.t()} {state.position:.0%} {state.displaytime} remaining"
        logging.debug("%s sleeping %0.2ds - %s", procname, state.sleep, state.user.status)
        
        await asyncio.sleep(state.sleep)

    # ttl expired, clean up before exit
    logging.info("%s timed out, cleaning house", procname)
    state.user.watcherid = ""
    state.user.status = "inactive"
    await state.user.save()
    
    logging.info("%s exiting", procname)
    return procname
