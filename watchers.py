"""reaper task"""
import logging
import asyncio
import datetime
import pickle
from models import User, UpcomingQueue
from users import getuser
from queue_manager import trackinfo, getnext
from raters import rate, record
from spot_funcs import is_saved

# pylint: disable=trailing-whitespace
# pylint: disable=broad-exception-caught

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


async def watchman(taskset, cred, spotify, watcher, userid=None):
    """check the database regularly and start a watcher for active users"""
    procname="watchman"
    sleep = 10
    if userid is not None:
        user = await User.get(spotifyid=userid)
        
        logging.info("%s creating a spotify watcher task for: %s", 
                        procname, user.spotifyid)
        user_task = asyncio.create_task(watcher(cred, spotify, user.spotifyid),
                        name=f"watcher_{user.spotifyid}")
        
        # add this user task to the global tasks set
        added = taskset.add(user_task)
        if added is not None:
            logging.error("%s failed adding task to taskset?")
            return
        # To prevent keeping references to finished tasks forever,
        # make each task remove its own reference from the set after
        # completion:

        user_task.add_done_callback(taskset.remove(user_task))
        logging.info("%s task created, callback added", procname)

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


async def spotify_watcher(cred, spotify, userid):
    """start a long-running task to monitor a user's spotify usage, rate and record plays"""

    procname = f"watcher_{userid}"
    
    logging.info("%s watcher starting", procname)

    try:
        user, token = await getuser(cred, userid)
    except Exception as e: # pylint: disable=broad-exception-caught
        logging.error("%s getuser exception %s",procname, e)

    # check for a lock in the database from another watcher
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    timestamp = datetime.datetime.now().strftime('%s')
    watcherid = f"watcher_{user.spotifyid}_{timestamp}"
    
    if user.watcherid == "killswitch":
        logging.warning("%s detected killswitch, won't start, unsetting killswitch", procname)
        user.watcherid = ""
        await user.save()
        return "killswitch"
    
    # if (user.watcherid != watcherid
    #       and user.status != "inactive"
    #       and user.last_active > recent):
    #     logging.error("%s initial startup found a recent active watcher, won't start", procname)
    #     return "another active watcher"
    
    user.watcherid = watcherid
    await user.save()
    
    ttl = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=20)

    # Check the initial status
    with spotify.token_as(token):
        try:
            currently = await spotify.playback_currently_playing()
        except Exception as e: # pylint: disable=broad-exception-caught
            logging.error("%s spotify_currently_playing exception %s", procname, e)
        
        if currently is None:
            logging.debug("%s not currently playing", procname)
            sleep = 30
            # track = None
        elif currently.is_playing is False:
            logging.debug("%s paused", procname)
            sleep = 30
            # track = None
        else:
            
            trackid = currently.item.id
            trackname, _ = await trackinfo(spotify, trackid, return_track=True)
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            position = currently.progress_ms/currently.item.duration_ms
            
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60
            logging.info("%s initial status - playing %s, %s:%0.02d remaining",
                         procname, trackname, minutes, seconds)

    # Loop while alive
    logging.debug("%s starting loop", procname)
    while ttl > datetime.datetime.now(datetime.timezone.utc):
        
        logging.debug("%s loop is awake", procname)
        user = await User.get(spotifyid=userid)
        status = user.status
        
        if user.watcherid == "killswitch":
            logging.warning("%s detected killswitch, unsetting killswitch and exiting", procname)
            user.watcherid = ""
            await user.save()
            return "killswitch"
        
        if (user.watcherid != watcherid 
          and user.status != "inactive"
          and user.last_active > recent):
            logging.error("%s found another recent active watcher, exiting", procname)
            return "another active watcher"
 
        # refresh the spotify token if necessary
        if token.is_expiring:
            try:
                token = cred.refresh(token)
            except Exception as e:
                logging.error("getuser exception refreshing token\n%s", e)
            user.token = pickle.dumps(token)
            await user.save()

        logging.debug("%s checking currently playing", procname)
        with spotify.token_as(token):
            try:
                currently = await spotify.playback_currently_playing()
            except Exception as e:
                logging.error("%s exception in spotify.playback_currently_playing\n%s",procname, e)

        logging.debug("%s checking player queue state", procname)
        with spotify.token_as(token):
            playbackqueue = await spotify.playback_queue()
        playbackqueueids = [x.id for x in playbackqueue.queue]

        sleep = 30
        # not playing
        if currently is None:
            status = "not playing"
            trackid = None
            logging.debug("%s not currently playing", procname)

        # not playing - not paused?
        elif currently.currently_playing_type == "unknown":
            status = "not playing"
            trackid = None
            logging.debug("%s not currently playing", procname)

        # paused
        elif currently.is_playing is False:
            status = "paused"
            trackid = currently.item.id
            logging.debug("%s is paused", procname)

        # oh yeah now we cook
        else:

            ttl = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=20)
            logging.debug("%s updating ttl, last_active and status: %s", procname, ttl)
            user.last_active = datetime.datetime.now(datetime.timezone.utc)
            user.status = "active"
            await user.save()
            
            # note details from the last loop for comparison
            last_trackid = trackid
            last_position = position

            # pull details from the current item
            trackid = currently.item.id
            trackname, _ = await trackinfo(spotify, trackid, return_track=True)
            position = currently.progress_ms/currently.item.duration_ms
            
            # pull details for the next track in the queue
            nextup_tid, nextup_expires_at = await getnext()

            if nextup_tid is not None:
                nextup_name, nextup_track = await trackinfo(spotify, nextup_tid, return_track=True)
            
            # do some math
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60

            # we aren't in the endzone yet
            if remaining_ms > 30000:
                
                # we're playing the lead track in the Upcoming Queue
                # and nobody else has set the expiration
                if nextup_tid == trackid and nextup_expires_at == "":
                    # so we get to set it for our endzone
                    # which we can calculate pretty closely
                    expires_at = (datetime.datetime.now() + 
                                    datetime.timedelta(milliseconds=remaining_ms - 30000))
                    
                    _ = ( await UpcomingQueue.select_for_update()
                                                .filter(trackid=nextup_tid)
                                                .update(expires_at=expires_at))

                # detect track changes
                if trackid != last_trackid:
                    logging.info("%s detected track change at %.0d",
                                procname, last_position)
                    
                    # did we skip
                    if last_trackid == nextup_tid:
                        logging.warning("%s removing skipped track from radio queue: %s",
                                    procname, last_trackid)
                        try:
                            await UpcomingQueue.filter(trackid=nextup_tid).delete()
                        except Exception as e:
                            logging.error("%s exception removing track from queue\n%s",
                                        procname, e)
                    
                    # rate skipped tracks based on last position
                    if last_position < 0.33:
                        value = -2
                        logging.info("%s early skip rating, %s %s %s",
                                    userid, last_trackid, value, procname)
                        await rate(spotify, userid, last_trackid, value)
                    elif last_position < 0.7:
                        value = -1
                        logging.info("%s late skip rating, %s %s %s",
                                    userid, last_trackid, value, procname)
                        await rate(userid, last_trackid, value)
            
                if (remaining_ms - 30000) < 30000: # sleep for a few seconds
                    sleep = (remaining_ms - 30000) / 1000 # about to hit the autorate window
                else: # sleep for thirty seconds
                    sleep = 30

            elif remaining_ms <= 30000:
                # welcome to the end zone
                logging.info("%s endzone %s - next up %s",
                            procname, trackname, nextup_name)
                
                # we got to the end of the track, so autorate
                # base on whether or not this is a saved track
                value = 4 if await is_saved(spotify, token, trackid) else 1

                logging.info("%s setting a rating, %s %s %s", userid, trackid, value, procname)
                await rate(spotify, userid, trackid, value, autorate=True)
                
                # record in the playhistory table
                logging.debug("%s recording play history %s",
                                procname, trackname)
                await record(spotify, userid, trackid)

                # if we're finishing the Currently Playing queued track
                # we must be first to the endzone, remove track from dbqueue
                if trackid == nextup_tid:
                    logging.info("%s removing track from radio queue: %s",
                                procname, nextup_name)
                    try:
                        await UpcomingQueue.filter(trackid=nextup_tid).delete()
                    except Exception as e:
                        logging.error("%s exception removing track from upcomingqueue\n%s",
                                        procname, e)
                    
                    # get the next queued track
                    nextup_tid, nextup_expires_at = await getnext()
                    nextup_name, nextup_track = await trackinfo(spotify, 
                                                            nextup_tid, 
                                                            return_track=True)

                if nextup_tid in playbackqueueids:
                    # this next track is already in the queue (or context, annoyingly)
                    logging.info("%s next track already queued, don't requeue",
                                procname)
                    
                elif nextup_tid == trackid:
                    logging.info("%s next track currently playing, don't requeue",
                                procname)
                
                else:
                    # queue up the next track for this user
                    logging.info("%s sending to spotify queue %s",
                                    procname, nextup_name)
                    
                    with spotify.token_as(token):
                        try:
                            _ = await spotify.playback_queue_add(nextup_track.trackuri)
                        except Exception as e: 
                            logging.error(
                                "%s exception spotify.playback_queue_add track.uri=%s\n%s",
                                procname, nextup_name, e)

                # sleep until this track is done
                sleep = (remaining_ms /1000) + 2

            status = f"{trackname} {position:.0%} {minutes}:{seconds:0>2} remaining"

        if status == "not playing":
            logging.debug("%s sleeping %0.2ds - %s", procname, sleep, status)
        else:
            logging.info("%s sleeping %0.2ds - %s", procname, sleep, status)
        
        await asyncio.sleep(sleep)

    # ttl expired, clean up before exit
    logging.info("%s timed out, cleaning house", procname)
    user.watcherid = ""
    user.status = "inactive"
    await user.save()
    
    logging.info("%s exiting", procname)
    return procname
